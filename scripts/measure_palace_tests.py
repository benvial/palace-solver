"""Throwaway helpers for ticket 03 of .scratch/palace-tests. Never merged.

    snapshot ROOT OUT.json
    delta BEFORE.json AFTER.json OUT_PREFIX
    serial-regression CTEST.json OUT.json JOBS TIMEOUT
    compress ROOT DELTA_PREFIX            (needs `zstandard`)
    wheel-diff BEFORE_DIR AFTER_DIR OUT.txt
    summary MEASURE_DIR
"""

from __future__ import annotations

import concurrent.futures
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tarfile
import time
import xml.etree.ElementTree as ET
import zipfile

ERROR_LINE = re.compile(
    r"FAILED|failed|[Ee]rror|ERROR|[Aa]bort|Verification failed|terminate|"
    r"[Ee]xception|REQUIRE|CHECK|Segmentation|signal|Timeout|not supported"
)


def snapshot(root: str, out: str) -> None:
    files = {}
    root_path = pathlib.Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
            path = os.path.join(dirpath, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            rel = pathlib.Path(path).relative_to(root_path).as_posix()
            files[rel] = [st.st_size, st.st_mtime_ns]
    pathlib.Path(out).write_text(json.dumps(files))


def group(rel: str) -> str:
    parts = rel.split("/")
    if parts[0] == "superbuild" and len(parts) > 3:
        return "/".join(parts[:4])
    return "/".join(parts[:3])


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def delta(before: str, after: str, prefix: str) -> None:
    a = json.loads(pathlib.Path(before).read_text())
    b = json.loads(pathlib.Path(after).read_text())
    added = sorted(k for k in b if k not in a)
    changed = sorted(k for k in b if k in a and b[k] != a[k])
    removed = sorted(k for k in a if k not in b)
    grow = sum(b[k][0] for k in added) + sum(b[k][0] - a[k][0] for k in changed) - sum(a[k][0] for k in removed)
    by_group: dict[str, int] = {}
    for k in added:
        by_group[group(k)] = by_group.get(group(k), 0) + b[k][0]
    for k in changed:
        by_group[group(k)] = by_group.get(group(k), 0) + b[k][0] - a[k][0]
    lines = [
        f"total before: {human(sum(v[0] for v in a.values()))} in {len(a)} files",
        f"total after:  {human(sum(v[0] for v in b.values()))} in {len(b)} files",
        f"added {len(added)}, changed {len(changed)}, removed {len(removed)}; net growth {human(grow)}",
        "",
        "growth by directory (added + changed size delta):",
    ]
    for g, n in sorted(by_group.items(), key=lambda kv: -kv[1])[:40]:
        lines.append(f"  {human(n):>12}  {g}")
    pathlib.Path(prefix + ".txt").write_text("\n".join(lines) + "\n")
    pathlib.Path(prefix + "-files.txt").write_text("\n".join(added + changed) + "\n")
    print("\n".join(lines))


def serial_regression(ctest_json: str, out: str, jobs: str, timeout: str) -> None:
    """Each [Regression] case on one rank: ctest's command minus its launcher."""
    data = json.loads(pathlib.Path(ctest_json).read_text())
    cases = []
    for test in data["tests"]:
        props = {p["name"]: p["value"] for p in test.get("properties", [])}
        if "regression" not in (props.get("LABELS") or []):
            continue
        cmd = test.get("command") or []
        idx = next((i for i, c in enumerate(cmd) if pathlib.Path(c).name.startswith("palace-unit-tests")), None)
        if idx is None:
            continue
        env = dict(os.environ)
        for kv in props.get("ENVIRONMENT") or []:
            k, _, v = kv.partition("=")
            env[k] = v
        cases.append((test["name"], cmd[idx:], props.get("WORKING_DIRECTORY"), env))

    def run(case):
        name, cmd, cwd, env = case
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=float(timeout)
            )
            rc, output = proc.returncode, proc.stdout.decode(errors="replace")
        except subprocess.TimeoutExpired as exc:
            rc, output = "timeout", (exc.stdout or b"").decode(errors="replace")
        return {
            "name": name,
            "seconds": round(time.monotonic() - t0, 1),
            "exit": rc,
            "first_error": first_error(output) if rc not in (0, 4) else "",
            "tail": output[-3000:] if rc not in (0, 4) else "",
        }

    t0 = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=int(jobs)) as pool:
        results = list(pool.map(run, cases))
    report = {"wall_seconds": round(time.monotonic() - t0, 1), "cases": results}
    pathlib.Path(out).write_text(json.dumps(report, indent=1))
    failed = [r for r in results if r["exit"] not in (0, 4)]
    print(f"{len(results)} cases, {len(failed)} failed, wall {report['wall_seconds']} s")
    sys.exit(1 if failed else 0)


def first_error(output: str) -> str:
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    for ln in lines:
        if ERROR_LINE.search(ln) and not ln.startswith(("Randomness seeded", "Filters:")):
            return ln[:300]
    return lines[-1][:300] if lines else ""


def compress(root: str, prefix: str) -> None:
    import zstandard

    files = [f for f in pathlib.Path(prefix + "-files.txt").read_text().splitlines() if f]
    class Counter(io.RawIOBase):
        n = 0

        def writable(self):
            return True

        def write(self, b):
            self.n += len(b)
            return len(b)

    counter = Counter()
    cctx = zstandard.ZstdCompressor(level=3, threads=-1)
    with cctx.stream_writer(counter, closefd=False) as zw:
        with tarfile.open(fileobj=zw, mode="w|") as tar:
            for rel in files:
                path = pathlib.Path(root, rel)
                if path.exists() or path.is_symlink():
                    tar.add(str(path), arcname=rel, recursive=False)
    line = f"zstd-3 tar of the {len(files)} added/changed files: {human(counter.n)}"
    with open(prefix + ".txt", "a") as fh:
        fh.write(line + "\n")
    print(line)


def wheel_listing(directory: str) -> dict[str, int]:
    wheel = next(pathlib.Path(directory).glob("*.whl"))
    with zipfile.ZipFile(wheel) as zf:
        return {i.filename: i.file_size for i in zf.infolist()}


def wheel_diff(before: str, after: str, out: str) -> None:
    a, b = wheel_listing(before), wheel_listing(after)
    lines = [f"before: {len(a)} members; after: {len(b)} members"]
    lines += [f"+ {k} ({b[k]})" for k in sorted(set(b) - set(a))]
    lines += [f"- {k} ({a[k]})" for k in sorted(set(a) - set(b))]
    lines += [f"~ {k} ({a[k]} -> {b[k]})" for k in sorted(set(a) & set(b)) if a[k] != b[k]]
    if len(lines) == 1:
        lines.append("member names and sizes identical")
    pathlib.Path(out).write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


def junit_cases(path: pathlib.Path):
    root = ET.parse(path).getroot()
    for tc in root.iter("testcase"):
        status = tc.get("status") or "run"
        failure = tc.find("failure")
        skipped = tc.find("skipped")
        out = tc.findtext("system-out") or ""
        if failure is not None:
            status = "failed"
        elif skipped is not None or status in ("notrun", "disabled"):
            status = "skipped"
        else:
            status = "passed"
        yield tc.get("name"), status, float(tc.get("time") or 0), (first_error(out) if status == "failed" else "")


def summary(measure: str) -> None:
    m = pathlib.Path(measure)
    out = ["## Palace test measurement", ""]
    if (m / "facts.txt").exists():
        out += ["```", (m / "facts.txt").read_text().strip(), "```", ""]
    if (m / "times.tsv").exists():
        out += ["| step | seconds | exit |", "|---|---|---|"]
        for row in (m / "times.tsv").read_text().splitlines()[1:]:
            out.append("| " + " | ".join(row.split("\t")) + " |")
        out.append("")
    for name in ("delta-install", "delta-sweeps", "wheel-diff"):
        if (m / f"{name}.txt").exists():
            out += [f"### {name}", "```", (m / f"{name}.txt").read_text().strip(), "```", ""]
    for xml in sorted(m.glob("*.xml")):
        cases = list(junit_cases(xml))
        counts = {s: sum(1 for c in cases if c[1] == s) for s in ("passed", "failed", "skipped")}
        out += [
            f"### {xml.stem}: {counts} ; sum of test times {sum(c[2] for c in cases):.0f} s",
            "slowest: " + ", ".join(f"{n} {t:.0f}s" for n, _, t, _ in sorted(cases, key=lambda c: -c[2])[:8]),
            "",
        ]
        for n, s, t, e in cases:
            if s == "failed":
                out.append(f"- FAIL `{n}` ({t:.0f}s): {e}")
        out.append("")
    reg = m / "regression-serial.json"
    if reg.exists():
        data = json.loads(reg.read_text())
        cases = data["cases"]
        bad = [c for c in cases if c["exit"] not in (0, 4)]
        out += [
            f"### regression-serial: {len(cases)} cases, {len(bad)} failed, wall {data['wall_seconds']} s, "
            f"sum {sum(c['seconds'] for c in cases):.0f} s",
            "slowest: "
            + ", ".join(f"{c['name']} {c['seconds']:.0f}s" for c in sorted(cases, key=lambda c: -c["seconds"])[:8]),
            "",
        ]
        out += [f"- FAIL `{c['name']}` ({c['seconds']}s, exit {c['exit']}): {c['first_error']}" for c in bad]
        out.append("")
    if (m / "build-errors.txt").exists() and not (m / "installed").exists():
        out += ["### build errors (first 60)", "```", *(m / "build-errors.txt").read_text().splitlines()[:60], "```"]
    text = "\n".join(out) + "\n"
    (m / "summary.md").write_text(text)
    print(text)


if __name__ == "__main__":
    cmd, *args = sys.argv[1:]
    {
        "snapshot": snapshot,
        "delta": delta,
        "serial-regression": serial_regression,
        "compress": compress,
        "wheel-diff": wheel_diff,
        "summary": summary,
    }[cmd](*args)
