#!/usr/bin/env python3
"""Publish the merged package to one container by overlaying only changed modules.

The merged tree is the union of what each production container already runs, so a
release image is built FROM the currently running image and only the modules that
actually differ are copied in. Everything else -- mounts, limits, commands, state
and the other services -- stays byte-identical.

Stages: probe / diff / build / switch / verify. Each stage is idempotent and
re-reads state instead of replaying a side effect.
"""
import argparse
import base64
import hashlib
import io
import json
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "work/muli-sorter"
SRC = WORK / "src/muli_sorter"
OUT = ROOT / "outputs/仓库合并与统一上线"

SSH = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
       "-o", "ConnectTimeout=10", "muli-nas", "python3", "-"]

TARGETS = {
    "console": {
        "container": "muli-archive-archive-console-1",
        "base_image": "muli-sorter:console-history-fast-4-console",
        "target_image": "muli-sorter:merged-20261008h-console",
        "service": "archive-console",
        "release": "/vol2/1000/Docker/muli-sorter/releases/merged-20261008-console",
    },
    "classifier": {
        "container": "muli-sorter-muli-sorter-1",
        "base_image": "muli-sorter:dji-utc-fix-1-classifier",
        "target_image": "muli-sorter:merged-20261008h-classifier",
        "service": "muli-sorter",
        "release": "/vol2/1000/Docker/muli-sorter/releases/merged-20261008-classifier",
    },
}


def run(script, payload=None):
    raw = script.encode()
    if payload is not None:
        raw = script.replace("__PAYLOAD_B64__", base64.b64encode(payload).decode()).encode()
    proc = subprocess.run(SSH, input=raw, capture_output=True)
    out = proc.stdout.decode("utf-8", "replace").strip()
    err = proc.stderr.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        raise SystemExit("remote stage failed:\n%s\n%s" % (out[-2000:], err[-2000:]))
    if not out:
        raise SystemExit("remote stage produced no result\n%s" % err[-2000:])
    return json.loads(out.splitlines()[-1]), err


BASELINE = r'''
import hashlib, io, json, subprocess, tarfile
IMAGE = "__IMAGE__"
raw = subprocess.run(["docker", "run", "--rm", "-u", "0", "--entrypoint", "tar", IMAGE,
                      "-c", "-C", "/usr/local/lib/python3.12/site-packages", "muli_sorter"],
                     capture_output=True, check=True).stdout
tar = tarfile.open(fileobj=io.BytesIO(raw))
files = {}
for member in tar.getmembers():
    if not member.isfile() or "/__pycache__/" in member.name:
        continue
    name = member.name.split("/", 1)[1]
    files[name] = hashlib.sha256(tar.extractfile(member).read()).hexdigest()
print(json.dumps({"image": IMAGE, "modules": files}))
'''


def baseline_modules(image):
    return run(BASELINE.replace("__IMAGE__", image))[0]


def changed_modules(target):
    config = TARGETS[target]
    base = baseline_modules(config["base_image"])["modules"]
    candidate = {}
    for name, digest in base.items():
        path = SRC / name
        if not path.is_file():
            raise SystemExit("merged tree is missing %s" % name)
        mine = hashlib.sha256(path.read_bytes()).hexdigest()
        if mine != digest:
            candidate[name] = {"sha256": mine, "bytes": path.stat().st_size}
    # Carry the whole package in every container so the image and the repository
    # describe exactly the same files; services only ever import what they need.
    for path in sorted(SRC.rglob("*")):
        if not path.is_file() or "__pycache__" in str(path):
            continue
        name = str(path.relative_to(SRC))
        if name in base:
            continue
        candidate[name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                           "bytes": path.stat().st_size, "added": True}
    return base, candidate


def payload(target, config, candidate, base):
    files = {
        "Dockerfile": ("FROM %s\nCOPY candidate/ /usr/local/lib/python3.12/site-packages/muli_sorter/\n"
                       % config["base_image"]).encode(),
        "overlay.json": (json.dumps({"services": {config["service"]: {"image": config["target_image"]}}}) + "\n").encode(),
        "release-manifest.json": (json.dumps({
            "candidate": "merged-20261008-%s" % target,
            "base_image": config["base_image"],
            "target_image": config["target_image"],
            "scope": "packages merged from each container's live site-packages; mounts, limits, commands and other services unchanged",
            "modules": candidate,
            "base_module_count": len(base),
            "media_operations": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=False, indent=2) + "\n").encode(),
    }
    for name in candidate:
        files["candidate/" + name] = (SRC / name).read_bytes()
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for name in sorted(files):
            info = tarfile.TarInfo(name)
            info.size = len(files[name]); info.mode = 0o644; info.mtime = 0
            tar.addfile(info, io.BytesIO(files[name]))
    return raw.getvalue()


PROBE = r'''
import json, subprocess
CONTAINER = "__CONTAINER__"
def out(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()
info = json.loads(out(["docker", "inspect", CONTAINER, "--format", "{{json .}}"]))
labels = info["Config"].get("Labels") or {}
print(json.dumps({
    "container": CONTAINER,
    "image": info["Config"]["Image"],
    "image_id": info["Image"],
    "health": (info["State"].get("Health") or {}).get("Status"),
    "restarts": info["RestartCount"],
    "mounts": [{"source": m["Source"], "target": m["Destination"], "rw": m["RW"]} for m in info["Mounts"]],
    "compose": {k: labels.get("com.docker.compose." + k) for k in ("project", "service", "working_dir", "config_files")},
    "memory_bytes": info["HostConfig"].get("Memory"),
    "restart_policy": info["HostConfig"].get("RestartPolicy"),
    "network_mode": info["HostConfig"].get("NetworkMode"),
}))
'''

BUILD = r'''
import base64, json, os, subprocess, sys
release = "__RELEASE__"; image = "__IMAGE__"
payload = base64.b64decode("__PAYLOAD_B64__")
os.makedirs(release, exist_ok=True)
subprocess.run(["tar", "-xzf", "-", "-C", release], input=payload, check=True)
subprocess.run(["chmod", "-R", "a+rX", release + "/candidate"], check=True)
known = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
if known.returncode == 0:
    image_id = json.loads(subprocess.run(["docker", "image", "inspect", image, "--format", "{{json .Id}}"], capture_output=True, text=True, check=True).stdout)
    print(json.dumps({"built": False, "reason": "image already exists", "image_id": image_id}))
    raise SystemExit(0)
log = subprocess.run(["docker", "build", "--network", "none", "-t", image, release], capture_output=True, text=True)
open(release + "/build.log", "w").write(log.stdout + log.stderr)
if log.returncode != 0:
    print(json.dumps({"built": False, "error": log.stdout[-2000:] + log.stderr[-2000:]}))
    raise SystemExit(1)
image_id = json.loads(subprocess.run(["docker", "image", "inspect", image, "--format", "{{json .Id}}"], capture_output=True, text=True, check=True).stdout)
print(json.dumps({"built": True, "image_id": image_id}))
'''

SWITCH = r'''
import json, subprocess
from datetime import datetime, timezone
container = "__CONTAINER__"; image = "__IMAGE__"; release = "__RELEASE__"
def out(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()
labels = json.loads(out(["docker", "inspect", container, "--format", "{{json .Config.Labels}}"]))
project = labels["com.docker.compose.project"]
service = labels["com.docker.compose.service"]
working = labels["com.docker.compose.project.working_dir"]
configs = labels["com.docker.compose.project.config_files"].split(",")
def composed(extra):
    cmd = ["docker", "compose", "-p", project, "--project-directory", working]
    for path in configs:
        cmd += ["-f", path]
    for path in extra:
        cmd += ["-f", path]
    return cmd
before = json.loads(out(composed([]) + ["config", "--format", "json"]))
after = json.loads(out(composed([release + "/overlay.json"]) + ["config", "--format", "json"]))
expected = json.loads(json.dumps(before))
expected["services"][service]["image"] = image
if after != expected:
    print(json.dumps({"switched": False, "error": "compose difference beyond image",
                      "expected": expected["services"][service], "actual": after["services"][service]}))
    raise SystemExit(1)
running = json.loads(out(["docker", "inspect", container, "--format", "{{json .Config.Image}}"]))
if before["services"][service]["image"] == image and running == image:
    print(json.dumps({"switched": False, "reason": "already on target image"}))
    raise SystemExit(0)
json.dump({"container": container, "from": before["services"][service]["image"], "to": image,
           "intent_at": datetime.now(timezone.utc).isoformat()},
          open(release + "/deployment-intent.json", "w"), ensure_ascii=False, indent=2)
subprocess.run(composed([release + "/overlay.json"]) + ["up", "-d", "--no-deps", "--no-build", "--pull", "never", service], check=True)
print(json.dumps({"switched": True, "service": service, "to": image}))
'''

VERIFY = r'''
import json, subprocess, time
container = "__CONTAINER__"; image = "__IMAGE__"
def out(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()
deadline = time.time() + 150
state = {}
while time.time() < deadline:
    info = json.loads(out(["docker", "inspect", container, "--format", "{{json .}}"]))
    state = {"image": info["Config"]["Image"], "image_id": info["Image"],
             "running": info["State"]["Running"],
             "health": (info["State"].get("Health") or {}).get("Status"),
             "restarts": info["RestartCount"], "started_at": info["State"]["StartedAt"],
             "oom_killed": info["State"].get("OOMKilled")}
    if state["running"] and state["health"] == "healthy" and state["image"] == image:
        break
    time.sleep(5)
probe = ("import hashlib,json,pathlib;root=pathlib.Path('/usr/local/lib/python3.12/site-packages/muli_sorter');"
         "print(json.dumps({str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() "
         "for p in sorted(root.rglob('*')) if p.is_file() and '__pycache__' not in str(p)}))")
installed = json.loads(out(["docker", "run", "--rm", "-u", "0", "--entrypoint", "python", image, "-c", probe]))
print(json.dumps({"state": state, "installed": installed}))
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("target", choices=sorted(TARGETS))
    parser.add_argument("stage", choices=["probe", "diff", "build", "switch", "verify"])
    args = parser.parse_args()
    config = TARGETS[args.target]
    if args.stage == "probe":
        result, _ = run(PROBE.replace("__CONTAINER__", config["container"]))
    elif args.stage == "diff":
        base, candidate = changed_modules(args.target)
        result = {"base": config["base_image"], "base_modules": len(base),
                  "changed_count": len(candidate), "changed": candidate}
    elif args.stage == "build":
        base, candidate = changed_modules(args.target)
        script = BUILD.replace("__RELEASE__", config["release"]).replace("__IMAGE__", config["target_image"])
        result, _ = run(script, payload(args.target, config, candidate, base))
    elif args.stage == "switch":
        script = (SWITCH.replace("__CONTAINER__", config["container"])
                        .replace("__IMAGE__", config["target_image"])
                        .replace("__RELEASE__", config["release"]))
        result, _ = run(script)
    else:
        script = VERIFY.replace("__CONTAINER__", config["container"]).replace("__IMAGE__", config["target_image"])
        result, _ = run(script)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / ("%s-%s.json" % (args.target, args.stage))).write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    trimmed = {k: v for k, v in result.items() if k not in ("installed", "host_config", "env")}
    print(json.dumps(trimmed, ensure_ascii=False, indent=2)[:4000])


if __name__ == "__main__":
    main()
