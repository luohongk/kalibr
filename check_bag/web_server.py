#!/usr/bin/env python3
"""Small local web UI server for check_bag.

Run this from the Kalibr workspace, preferably inside the ROS/Kalibr
environment where ``rosbag`` is importable:

    python3 check_bag/web_server.py --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import argparse
import io
import json
import mimetypes
import os
import posixpath
import queue
import shutil
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse


WORKSPACE = Path(__file__).resolve().parents[1]
DEFAULT_UPLOAD_DIR = WORKSPACE / "check_bag" / "calib_data"
DEFAULT_REPORT_ROOT = WORKSPACE / "check_bag" / "output"
DEFAULT_COSCLI_PATH = Path("/home/coscli")
DEFAULT_COS_BASE = "cos://home/conanluo/okvis_data"
DOCKER_IMAGE = "luohongkun0715/kalibr_and_imu_utils:noetic"
CONTAINER_WORKSPACE = Path("/catkin_ws/src/kalibr")
DOCKER_DATA_VOLUME = "kalibr_check_bag_data"
CONTAINER_DATA_ROOT = Path("/data")


@dataclass
class Job:
    job_id: str
    upload_path: Path
    report_dir: Path
    command: List[str]
    created_at: float = field(default_factory=time.time)
    status: str = "queued"
    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    upload_action: str = ""
    upload_count: int = 0
    upload_bytes: int = 0
    source: str = ""
    finished_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        report_ready = (self.report_dir / "index.html").exists()
        return {
            "job_id": self.job_id,
            "status": self.status,
            "returncode": self.returncode,
            "command": self.command,
            "stdout": self.stdout[-20000:],
            "stderr": self.stderr[-20000:],
            "error": self.error,
            "upload_path": str(self.upload_path),
            "report_dir": str(self.report_dir),
            "upload_action": self.upload_action,
            "upload_count": self.upload_count,
            "upload_bytes": self.upload_bytes,
            "source": self.source,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "report_ready": report_ready,
            "report_url": f"/report_view.html?job={self.job_id}" if report_ready else "",
            "raw_report_url": f"/reports/{self.job_id}/index.html" if report_ready else "",
            "report_json_url": f"/reports/{self.job_id}/report.json" if (self.report_dir / "report.json").exists() else "",
        }


class CheckBagServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], handler: type[BaseHTTPRequestHandler], upload_dir: Path):
        super().__init__(server_address, handler)
        self.upload_dir = upload_dir
        self.jobs: Dict[str, Job] = {}
        self.lock = threading.Lock()

    def add_job(self, job: Job) -> None:
        with self.lock:
            self.jobs[job.job_id] = job

    def get_job(self, job_id: str) -> Optional[Job]:
        with self.lock:
            return self.jobs.get(job_id)


@dataclass
class FormItem:
    name: str
    value: str
    filename: str = ""
    file: io.BytesIO = field(default_factory=io.BytesIO)


class FormData:
    def __init__(self) -> None:
        self._items: Dict[str, FormItem | List[FormItem]] = {}

    def add(self, item: FormItem) -> None:
        existing = self._items.get(item.name)
        if existing is None:
            self._items[item.name] = item
        elif isinstance(existing, list):
            existing.append(item)
        else:
            self._items[item.name] = [existing, item]

    def __contains__(self, name: str) -> bool:
        return name in self._items

    def __getitem__(self, name: str) -> FormItem | List[FormItem]:
        return self._items[name]


def parse_form(headers: Any, rfile: Any) -> FormData:
    form = FormData()
    content_type = headers.get("Content-Type", "")
    try:
        content_length = int(headers.get("Content-Length", "0"))
    except ValueError:
        content_length = 0
    body = rfile.read(max(content_length, 0))

    if content_type.startswith("multipart/form-data"):
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + content_type.encode("utf-8") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
        )
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            filename = part.get_filename() or ""
            payload = part.get_payload(decode=True) or b""
            if filename:
                value = ""
            else:
                charset = part.get_content_charset() or "utf-8"
                value = payload.decode(charset, errors="replace")
            form.add(FormItem(str(name), value, str(filename), io.BytesIO(payload)))
        return form

    if content_type.startswith("application/x-www-form-urlencoded"):
        values = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        for name, field_values in values.items():
            for value in field_values:
                form.add(FormItem(name, value))
        return form

    return form


class Handler(BaseHTTPRequestHandler):
    server: CheckBagServer

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[check_bag_web] {self.address_string()} - {fmt % args}", file=sys.stderr)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in {"/", "/index.html"}:
            self.serve_file(WORKSPACE / "index.html")
            return
        if path == "/report_view.html":
            self.serve_file(WORKSPACE / "check_bag" / "report_view.html")
            return
        if path == "/api/cos/datasets":
            self.handle_cos_datasets()
            return
        if path.startswith("/api/jobs/"):
            self.handle_get_job(path)
            return
        if path.startswith("/reports/"):
            self.handle_report_file(path)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/cos/pull":
            self.handle_cos_pull()
            return
        if parsed.path == "/api/jobs":
            self.handle_create_job()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def handle_get_job(self, path: str) -> None:
        job_id = path.rstrip("/").split("/")[-1]
        job = self.server.get_job(job_id)
        if not job:
            payload = report_job_from_disk(job_id)
            if payload is None:
                self.send_error(HTTPStatus.NOT_FOUND, "Unknown job")
                return
            self.send_json(payload)
            return
        self.send_json(job.to_dict())

    def handle_cos_datasets(self) -> None:
        try:
            datasets = list_cos_datasets()
        except ValueError as exc:
            self.send_json({"error": str(exc), "datasets": []}, HTTPStatus.BAD_REQUEST)
            return
        self.send_json({"cos_base": DEFAULT_COS_BASE, "datasets": datasets})

    def handle_cos_pull(self) -> None:
        form = parse_form(self.headers, self.rfile)
        cos_path = get_form_value(form, "cos_path", "")
        if not cos_path:
            self.send_json({"error": "missing cos_path"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            dataset_name = infer_cos_dataset_name(form, cos_path)
            dataset_dir = self.server.upload_dir / dataset_name
            action, file_count, total_bytes = sync_cos_dataset(form, dataset_dir, cos_path)
            bags = list_bags_in_volume(dataset_name)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self.send_json(
            {
                "status": "pulled",
                "dataset_name": dataset_name,
                "cos_path": cos_path,
                "dataset_dir": str(CONTAINER_DATA_ROOT / dataset_name),
                "action": action,
                "file_count": file_count,
                "total_bytes": total_bytes,
                "bags": bags,
            }
        )

    def handle_create_job(self) -> None:
        form = parse_form(self.headers, self.rfile)

        job_id = uuid.uuid4().hex[:12]
        try:
            cos_path = get_form_value(form, "cos_path", "")
            if not cos_path:
                self.send_json({"error": "missing cos_path"}, HTTPStatus.BAD_REQUEST)
                return
            dataset_name = infer_cos_dataset_name(form, cos_path)
            dataset_dir = self.server.upload_dir / dataset_name
            upload_action, upload_count, upload_bytes = sync_cos_dataset(form, dataset_dir, cos_path)
            upload_path = select_bag_from_dir(form, dataset_dir)
            source = cos_path
        except ValueError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        report_root = Path(get_form_value(form, "report_root", str(DEFAULT_REPORT_ROOT))).expanduser()
        report_dir = report_root / job_id
        command = build_command(form, upload_path, report_dir)
        job = Job(
            job_id=job_id,
            upload_path=upload_path,
            report_dir=report_dir,
            command=command,
            upload_action=upload_action,
            upload_count=upload_count,
            upload_bytes=upload_bytes,
            source=source,
        )
        self.server.add_job(job)

        thread = threading.Thread(target=run_job, args=(job,), daemon=True)
        thread.start()
        self.send_json(job.to_dict(), HTTPStatus.ACCEPTED)

    def handle_report_file(self, path: str) -> None:
        parts = path.split("/", 3)
        if len(parts) < 3:
            self.send_error(HTTPStatus.NOT_FOUND, "Missing report job")
            return
        job_id = parts[2]
        rel = parts[3] if len(parts) > 3 else "index.html"
        job = self.server.get_job(job_id)
        safe_rel = safe_relative_path(rel)
        if safe_rel is None:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid report path")
            return
        report_dir = job.report_dir if job else DEFAULT_REPORT_ROOT / safe_name(job_id)
        self.serve_file(report_dir / safe_rel)

    def serve_file(self, path: Path) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload: Dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def get_form_value(form: FormData, name: str, default: str = "") -> str:
    if name not in form:
        return default
    item = form[name]
    if isinstance(item, list):
        item = item[0]
    value = getattr(item, "value", default)
    return str(value).strip()


def get_form_bool(form: FormData, name: str, default: bool = False) -> bool:
    value = get_form_value(form, name, "true" if default else "false").lower()
    return value in {"1", "true", "yes", "on"}


def collect_upload_items(form: FormData) -> List[FormItem]:
    items: List[FormItem] = []
    for name in ("files", "bag", "bag_file"):
        if name not in form:
            continue
        value = form[name]
        candidates = value if isinstance(value, list) else [value]
        for item in candidates:
            if getattr(item, "filename", ""):
                items.append(item)
    return items


def infer_dataset_name(form: FormData, items: List[FormItem]) -> str:
    requested = get_form_value(form, "dataset_name", "")
    if requested:
        return safe_name(requested)

    first = normalize_filename(items[0].filename)
    parts = [part for part in PurePosixPath(first).parts if part not in {"", "."}]
    if len(parts) > 1:
        return safe_name(parts[0])
    return safe_name(Path(parts[0]).stem if parts else f"upload-{uuid.uuid4().hex[:8]}")


def infer_cos_dataset_name(form: FormData, cos_path: str) -> str:
    requested = get_form_value(form, "dataset_name", "")
    if requested:
        return safe_name(requested)
    stripped = cos_path.rstrip("/")
    name = stripped.rsplit("/", 1)[-1] if "/" in stripped else stripped
    return safe_name(name)


def safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in value.strip())
    cleaned = cleaned.strip("._")
    if not cleaned:
        raise ValueError("invalid dataset name")
    return cleaned[:120]


def normalize_filename(value: str) -> str:
    return str(value or "").replace("\\", "/").lstrip("/")


def safe_relative_upload_path(filename: str, dataset_name: str) -> Path:
    normalized = normalize_filename(filename)
    raw_parts = [part for part in PurePosixPath(normalized).parts if part not in {"", "."}]
    if len(raw_parts) > 1:
        raw_parts = raw_parts[1:]
    parts = []
    for part in raw_parts:
        if part == "..":
            raise ValueError(f"invalid upload path: {filename}")
        parts.append(safe_name(part))
    if not parts:
        raise ValueError(f"invalid upload filename: {filename}")
    return Path(*parts)


def uploaded_file_size(item: FormItem) -> int:
    stream = item.file
    current = stream.tell()
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(current)
    return int(size)


def build_upload_entries(items: List[FormItem], dataset_name: str) -> List[Tuple[FormItem, Path, int]]:
    entries: List[Tuple[FormItem, Path, int]] = []
    seen = set()
    for item in items:
        rel_path = safe_relative_upload_path(item.filename, dataset_name)
        if rel_path in seen:
            raise ValueError(f"duplicate upload file: {rel_path}")
        seen.add(rel_path)
        entries.append((item, rel_path, uploaded_file_size(item)))
    if not entries:
        raise ValueError("no upload files")
    return entries


def select_bag_path(form: FormData, entries: List[Tuple[FormItem, Path, int]]) -> Path:
    requested = get_form_value(form, "selected_bag", "")
    if requested:
        requested_path = Path(normalize_filename(requested))
        for _, rel_path, _ in entries:
            if rel_path == requested_path:
                return rel_path
        raise ValueError(f"selected bag was not uploaded: {requested}")

    bags = [rel_path for _, rel_path, _ in entries if rel_path.name.endswith(".bag")]
    if not bags:
        raise ValueError("uploaded data does not contain a .bag file")
    return sorted(bags, key=lambda p: str(p))[0]


def manifest_for_dir(root: Path) -> Dict[str, int]:
    if not root.is_dir():
        return {}
    manifest: Dict[str, int] = {}
    for path in root.rglob("*"):
        if path.is_file():
            manifest[path.relative_to(root).as_posix()] = path.stat().st_size
    return manifest


def expected_manifest(entries: List[Tuple[FormItem, Path, int]]) -> Dict[str, int]:
    return {rel_path.as_posix(): size for _, rel_path, size in entries}


def sync_upload_dataset(root: Path, entries: List[Tuple[FormItem, Path, int]]) -> Tuple[str, int, int]:
    expected = expected_manifest(entries)
    existing = manifest_for_dir(root)
    if existing == expected and expected:
        action = "skipped_existing"
    else:
        action = "uploaded" if not existing else "reuploaded_after_cleanup"
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True, exist_ok=True)
        for item, rel_path, _ in entries:
            target = root / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            item.file.seek(0)
            with target.open("wb") as out:
                shutil.copyfileobj(item.file, out)

    actual = manifest_for_dir(root)
    if actual != expected:
        raise ValueError(
            f"upload verification failed: expected {len(expected)} files/{sum(expected.values())} bytes, "
            f"got {len(actual)} files/{sum(actual.values())} bytes"
        )
    return action, len(actual), sum(actual.values())


def parse_optional_int(value: str) -> Optional[int]:
    value = value.strip()
    if not value:
        return None
    try:
        number = int(value)
    except ValueError as exc:
        raise ValueError(f"invalid integer: {value}") from exc
    if number < 0:
        raise ValueError(f"invalid negative integer: {value}")
    return number


def dir_file_count_and_bytes(root: Path) -> Tuple[int, int]:
    manifest = manifest_for_dir(root)
    return len(manifest), sum(manifest.values())


def run_container_script(
    script: str,
    *,
    copy_check_bag: bool = False,
    copy_cos: bool = False,
    copy_report_to: Optional[Path] = None,
    report_container_path: Optional[str] = None,
    stream_job: Optional[Job] = None,
) -> subprocess.CompletedProcess[str]:
    name = f"checkbag_{uuid.uuid4().hex[:12]}"
    create_cmd = [
        "docker",
        "create",
        "--name",
        name,
        "--entrypoint",
        "/bin/bash",
        "--mount",
        f"type=volume,source={DOCKER_DATA_VOLUME},target={CONTAINER_DATA_ROOT}",
        DOCKER_IMAGE,
        "-lc",
        script,
    ]
    subprocess.run(create_cmd, cwd=str(WORKSPACE), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    try:
        if copy_cos:
            subprocess.run(["docker", "cp", str(DEFAULT_COSCLI_PATH), f"{name}:/usr/local/bin/coscli"], check=True)
            subprocess.run(["docker", "cp", "/root/.cos.yaml", f"{name}:/root/.cos.yaml"], check=True)
        if copy_check_bag:
            with tempfile.TemporaryDirectory(prefix="check_bag_src_") as tmpdir:
                stage = Path(tmpdir) / "check_bag"
                stage.mkdir()
                for filename in ("check_bag.py", "__init__.py"):
                    shutil.copy2(WORKSPACE / "check_bag" / filename, stage / filename)
                shutil.copytree(WORKSPACE / "check_bag" / "config", stage / "config")
                subprocess.run(["docker", "cp", str(stage), f"{name}:/tmp/check_bag"], check=True)
        if stream_job is None:
            result = subprocess.run(
                ["docker", "start", "-a", name],
                cwd=str(WORKSPACE),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        else:
            stream_job.stdout += f"[container] started {name}\n"
            proc = subprocess.Popen(
                ["docker", "start", "-a", name],
                cwd=str(WORKSPACE),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            output_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
            threads = [
                threading.Thread(target=read_stream, args=("stdout", proc.stdout, output_queue), daemon=True),
                threading.Thread(target=read_stream, args=("stderr", proc.stderr, output_queue), daemon=True),
            ]
            for thread in threads:
                thread.start()
            while proc.poll() is None or any(thread.is_alive() for thread in threads):
                drain_output(stream_job, output_queue)
                time.sleep(0.1)
            drain_output(stream_job, output_queue)
            result = subprocess.CompletedProcess(
                ["docker", "start", "-a", name],
                int(proc.returncode or 0),
                stream_job.stdout,
                stream_job.stderr,
            )
        if copy_report_to and report_container_path:
            if copy_report_to.exists():
                shutil.rmtree(copy_report_to)
            copy_report_to.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["docker", "cp", f"{name}:{report_container_path}", str(copy_report_to)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return result
    finally:
        subprocess.run(["docker", "rm", "-f", name], cwd=str(WORKSPACE), stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def parse_marker_json(output: str, marker: str) -> Dict[str, Any]:
    for line in reversed(output.splitlines()):
        if line.startswith(marker):
            return json.loads(line[len(marker):])
    raise ValueError(f"container did not emit {marker}")


def report_job_from_disk(job_id: str) -> Optional[Dict[str, Any]]:
    try:
        safe_job_id = safe_name(job_id)
    except ValueError:
        return None
    if safe_job_id != job_id:
        return None
    report_dir = DEFAULT_REPORT_ROOT / safe_job_id
    index_path = report_dir / "index.html"
    if not index_path.is_file():
        return None
    return {
        "job_id": safe_job_id,
        "status": "passed",
        "returncode": 0,
        "command": [],
        "stdout": "",
        "stderr": "",
        "error": "",
        "upload_path": "-",
        "report_dir": str(report_dir),
        "upload_action": "",
        "upload_count": 0,
        "upload_bytes": 0,
        "source": "",
        "created_at": report_dir.stat().st_mtime,
        "finished_at": index_path.stat().st_mtime,
        "report_ready": True,
        "report_url": f"/report_view.html?job={safe_job_id}",
        "raw_report_url": f"/reports/{safe_job_id}/index.html",
        "report_json_url": f"/reports/{safe_job_id}/report.json" if (report_dir / "report.json").exists() else "",
    }


def list_cos_datasets() -> List[Dict[str, str]]:
    if not DEFAULT_COSCLI_PATH.is_file():
        raise ValueError(f"coscli not found: {DEFAULT_COSCLI_PATH}")
    proc = subprocess.run(
        [str(DEFAULT_COSCLI_PATH), "ls", DEFAULT_COS_BASE.rstrip("/") + "/"],
        cwd=str(WORKSPACE),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise ValueError(f"coscli ls failed: {proc.stderr.strip() or proc.stdout.strip()}")

    datasets: List[Dict[str, str]] = []
    prefix = "conanluo/okvis_data/"
    seen = set()
    for line in proc.stdout.splitlines():
        if "|" not in line or " DIR " not in line:
            continue
        key = line.split("|", 1)[0].strip()
        if not key.startswith(prefix):
            continue
        name = key[len(prefix):].strip("/")
        if not name or "/" in name or name in seen:
            continue
        seen.add(name)
        datasets.append({"name": name, "cos_path": DEFAULT_COS_BASE.rstrip("/") + "/" + name})
    datasets.sort(key=lambda item: item["name"])
    return datasets


def list_bags(root: Path) -> List[str]:
    if not root.is_dir():
        return []
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*.bag") if path.is_file())


def sync_cos_dataset(form: FormData, root: Path, cos_path: str) -> Tuple[str, int, int]:
    force_pull = get_form_bool(form, "force_pull", False)
    dataset_name = root.name
    opts = shlex.split(get_form_value(form, "coscli_opts", "--routines 32 --part-size 500"))
    opts_literal = " ".join(shlex.quote(part) for part in opts)
    script = f"""
set -e
chmod +x /usr/local/bin/coscli
dataset={shlex.quote(dataset_name)}
target="/data/$dataset"
action="skipped_existing"
if [ {shlex.quote('true' if force_pull else 'false')} = true ] || [ ! -d "$target" ] || [ -z "$(find "$target" -type f -print -quit 2>/dev/null)" ]; then
  rm -rf "$target"
  mkdir -p /data
  /usr/local/bin/coscli cp {shlex.quote(cos_path.rstrip('/'))} "$target" -r {opts_literal}
  action="pulled_from_cos"
fi
count=$(find "$target" -type f | wc -l)
bytes=$(du -sb "$target" | cut -f1)
if [ "$count" = "0" ]; then
  echo "coscli pull produced no files: $target" >&2
  exit 2
fi
python3 - <<PY
import json
print("__CHECK_BAG_PULL__" + json.dumps({{"action": "$action", "count": int("$count"), "bytes": int("$bytes")}}))
PY
"""
    proc = run_container_script(script, copy_cos=True)
    if proc.returncode != 0:
        raise ValueError(f"coscli pull failed: {proc.stderr.strip() or proc.stdout.strip()}")
    result = parse_marker_json(proc.stdout, "__CHECK_BAG_PULL__")
    return str(result["action"]), int(result["count"]), int(result["bytes"])


def select_bag_from_dir(form: FormData, root: Path) -> Path:
    requested = get_form_value(form, "selected_bag", "")
    if requested:
        return CONTAINER_DATA_ROOT / root.name / normalize_filename(requested)

    bags = list_bags_in_volume(root.name)
    if not bags:
        raise ValueError(f"no .bag file found under dataset: {root.name}")
    return CONTAINER_DATA_ROOT / root.name / bags[0]


def list_bags_in_volume(dataset_name: str) -> List[str]:
    script = f"""
set -e
dataset={shlex.quote(dataset_name)}
target="/data/$dataset"
if [ ! -d "$target" ]; then
  echo "__CHECK_BAG_BAGS__[]"
  exit 0
fi
python3 - <<PY
import json
from pathlib import Path
root = Path({repr('/data/' + dataset_name)})
bags = sorted(str(p.relative_to(root)) for p in root.rglob('*.bag') if p.is_file())
print("__CHECK_BAG_BAGS__" + json.dumps(bags))
PY
"""
    proc = run_container_script(script)
    if proc.returncode != 0:
        raise ValueError(f"bag scan failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return list(parse_marker_json(proc.stdout, "__CHECK_BAG_BAGS__"))


def append_option(command: List[str], flag: str, value: str) -> None:
    value = value.strip()
    if value:
        command.extend([flag, value])


def container_path(path: Path) -> str:
    if str(path).startswith(str(CONTAINER_DATA_ROOT) + "/"):
        return str(path)
    resolved = path.expanduser().resolve()
    try:
        rel = resolved.relative_to(WORKSPACE)
    except ValueError:
        return str(path)
    return str(CONTAINER_WORKSPACE / rel)


def container_arg_path(value: str) -> str:
    if not value:
        return value
    path = Path(value).expanduser()
    try:
        return container_path(path)
    except RuntimeError:
        return value


def build_command(form: FormData, bag_path: Path, report_dir: Path) -> List[str]:
    script = CONTAINER_WORKSPACE / "check_bag" / "check_bag.py"
    report_container = "/tmp/check_bag_report"
    inner = ["python3", str(script), "--bag", container_path(bag_path)]
    target = get_form_value(form, "target", "")
    if target:
        inner.extend(["--target", container_arg_path(target)])
    if get_form_bool(form, "autodiscover", True):
        inner.append("--autodiscover")
    append_option(inner, "--bag-freq", get_form_value(form, "bag_freq", ""))
    append_option(inner, "--progress-interval", get_form_value(form, "progress_interval", ""))
    append_option(inner, "--target-workers", get_form_value(form, "target_workers", ""))
    inner.extend(["--report-dir", report_container])

    extra_args = get_form_value(form, "extra_args", "")
    if extra_args:
        inner.extend(shlex.split(extra_args))

    return ["__docker_check__", json.dumps({"inner": inner, "report_container": report_container})]


def run_job(job: Job) -> None:
    job.status = "running"
    try:
        job.report_dir.mkdir(parents=True, exist_ok=True)
        if job.command and job.command[0] == "__docker_check__":
            payload = json.loads(job.command[1])
            inner = payload["inner"]
            report_container = payload["report_container"]
            inner_command = " ".join(shlex.quote(part) for part in inner)
            script = f"""
set -e
rm -rf {shlex.quote(str(CONTAINER_WORKSPACE / 'check_bag'))}
mkdir -p {shlex.quote(str(CONTAINER_WORKSPACE))}
cp -a /tmp/check_bag {shlex.quote(str(CONTAINER_WORKSPACE / 'check_bag'))}
source /opt/ros/noetic/setup.bash
set +e
{inner_command}
rc=$?
exit $rc
"""
            proc = run_container_script(
                script,
                copy_check_bag=True,
                copy_report_to=job.report_dir,
                report_container_path=report_container,
                stream_job=job,
            )
            job.stdout = proc.stdout[-40000:]
            job.stderr = proc.stderr[-40000:]
            job.returncode = proc.returncode
            job.status = "passed" if proc.returncode == 0 else "failed"
            return

        proc = subprocess.Popen(
            job.command,
            cwd=str(WORKSPACE),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        output_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        threads = [
            threading.Thread(target=read_stream, args=("stdout", proc.stdout, output_queue), daemon=True),
            threading.Thread(target=read_stream, args=("stderr", proc.stderr, output_queue), daemon=True),
        ]
        for thread in threads:
            thread.start()

        while proc.poll() is None or any(thread.is_alive() for thread in threads):
            drain_output(job, output_queue)
            time.sleep(0.1)
        drain_output(job, output_queue)

        job.returncode = proc.returncode
        job.status = "passed" if proc.returncode == 0 else "failed"
    except Exception as exc:  # pragma: no cover - defensive for server runtime
        job.error = str(exc)
        job.status = "error"
        job.returncode = 2
    finally:
        job.finished_at = time.time()


def read_stream(name: str, stream: Any, output_queue: "queue.Queue[tuple[str, str]]") -> None:
    if stream is None:
        return
    for line in stream:
        output_queue.put((name, line))
    stream.close()


def drain_output(job: Job, output_queue: "queue.Queue[tuple[str, str]]") -> None:
    while True:
        try:
            name, line = output_queue.get_nowait()
        except queue.Empty:
            return
        if name == "stdout":
            job.stdout += line
            job.stdout = job.stdout[-40000:]
        else:
            job.stderr += line
            job.stderr = job.stderr[-40000:]


def safe_relative_path(value: str) -> Optional[Path]:
    value = unquote(value)
    normalized = posixpath.normpath("/" + value).lstrip("/")
    if normalized.startswith("../") or normalized == "..":
        return None
    return Path(normalized)


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the check_bag upload UI.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--upload-dir", default=str(DEFAULT_UPLOAD_DIR))
    args = parser.parse_args()

    upload_dir = Path(args.upload_dir).expanduser()
    upload_dir.mkdir(parents=True, exist_ok=True)
    server = CheckBagServer((args.host, args.port), Handler, upload_dir)
    print(f"Serving check_bag UI on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping check_bag UI server.", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
