#!/usr/bin/env python3
"""批量自动标定 Web 服务 (auto_calib web)。

在宿主机运行, 通过 `docker exec <container>` 复用已在运行的长驻容器
(默认 kalibr_work) 调 run_one.sh 做标定, 数据在容器 /data/<folder>/。

    python3 auto_calib/web_server.py --host 0.0.0.0 --port 8090

与 check_bag(8080, 临时容器 checkbag_<uuid>, volume kalibr_check_bag_data)
完全隔离: 端口 8090, 复用 kalibr_work + 宿主机挂载的 /data, 脚本拷到 /opt/auto_calib。
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import posixpath
import queue
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse


WORKSPACE = Path(__file__).resolve().parents[1]
WEB_DIR = Path(__file__).resolve().parent / "web"
SCRIPT_DIR = Path(__file__).resolve().parent
BOARD_DIR = WORKSPACE / "calibration_board_data"

# 运行期可被命令行覆盖的全局配置
CONTAINER = "kalibr_work"
DATA_ROOT = "/data"                 # 容器内数据根
CONTAINER_SCRIPTS = "/opt/auto_calib"

# 透传给 run_one.sh 的可调环境变量 (与 run_all.sh 的 export_vars 对齐)
ENV_KEYS = [
    "CAM_HZ", "BAG_FREQ", "IMU_RATE", "IMU_SAFETY", "IMU_TOPIC",
    "MODELS", "TARGET_NAME", "TARGET_SRC", "PLAY_RATE",
    "KEEP_CONVERTED", "REPROJ_WARN", "RESULT_KEEP",
]

RESULT_YAML_CAMCHAIN = "kalibr_input-camchain.yaml"
RESULT_YAML_IMUCAM = "kalibr_input-camchain-imucam.yaml"


# --------------------------------------------------------------------------
# Job 模型
# --------------------------------------------------------------------------
@dataclass
class Job:
    job_id: str
    folders: List[str]
    env: Dict[str, str]
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    status: str = "queued"            # queued/running/passed/failed/canceled
    current: str = ""                 # 当前正在标定的文件夹
    stdout_raw: str = ""              # 原始 stdout (含 \r, 全量不截断)
    stderr_raw: str = ""              # 原始 stderr
    stdout: str = ""                  # 归一化后用于展示 (全量)
    stderr: str = ""
    error: str = ""
    returncode: Optional[int] = None
    results: Dict[str, str] = field(default_factory=dict)   # folder -> ok/fail
    cancel_requested: bool = False
    process: Optional[subprocess.Popen] = field(default=None, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "folders": self.folders,
            "env": self.env,
            "status": self.status,
            "current": self.current,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error": self.error,
            "returncode": self.returncode,
            "results": self.results,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "cancel_requested": self.cancel_requested,
        }


class CalibServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, handler):
        super().__init__(server_address, handler)
        self.jobs: Dict[str, Job] = {}
        self.lock = threading.Lock()

    def add_job(self, job: Job) -> None:
        with self.lock:
            self.jobs[job.job_id] = job

    def get_job(self, job_id: str) -> Optional[Job]:
        with self.lock:
            return self.jobs.get(job_id)

    def list_jobs(self) -> List[Job]:
        with self.lock:
            return sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)


# --------------------------------------------------------------------------
# docker 调用辅助
# --------------------------------------------------------------------------
def docker_exec(args: List[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", CONTAINER] + args,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw,
    )


def container_running() -> bool:
    p = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return CONTAINER in p.stdout.split()


def read_stream(name: str, stream: Any, q: "queue.Queue[tuple[str, str]]") -> None:
    r"""按字节块读取原始流。

    Kalibr 的进度条用 \r (回车) 原地刷新而非 \n。若用文本模式(text=True)读, Python 的
    "通用换行"会把 \r 也翻译成 \n, 导致每次进度刷新变成一整行, 日志被撑爆且看似混乱。
    因此必须用二进制管道读原始字节(保留真正的 \r), 自行解码, 再由 _normalize_cr 折叠。
    """
    if stream is None:
        return
    try:
        while True:
            chunk = stream.read(512)          # bytes
            if not chunk:
                break
            q.put((name, chunk.decode("utf-8", errors="replace")))
    finally:
        stream.close()


# ANSI 颜色/控制转义序列 (如 run_one.sh 里的 \033[1;36m ... \033[0m)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _normalize_cr(text: str) -> str:
    r"""清理日志: 去掉 ANSI 转义码, 并把 \r 进度刷新折叠成可读多行。

    - 去除 "\x1b[1;36m" 一类颜色码 (网页 <pre> 不解析, 否则显示成乱码 ␛[..m)
    - "a\rb\rc\n"  -> 只保留最后一次刷新 "c\n" (同一行的多次覆盖)
    - 末尾未换行的 \r 段保留为一行, 以便前端看到当前进度。
    """
    text = _ANSI_RE.sub("", text)
    out_lines: List[str] = []
    # 先按 \n 切段, 每段内部再处理 \r (取最后一次覆盖结果)
    for seg in text.split("\n"):
        if "\r" in seg:
            seg = seg.split("\r")[-1]
        out_lines.append(seg)
    return "\n".join(out_lines)


def drain_output(job: Job, q: "queue.Queue[tuple[str, str]]") -> None:
    got = False
    while True:
        try:
            name, chunk = q.get_nowait()
        except queue.Empty:
            break
        got = True
        if name == "stdout":
            job.stdout_raw += chunk
        else:
            job.stderr_raw += chunk
    if got:
        # 每次有新数据就重算归一化后的完整日志 (保留全量, 不截断)
        job.stdout = _normalize_cr(job.stdout_raw)
        job.stderr = _normalize_cr(job.stderr_raw)


def sync_scripts_into_container() -> None:
    """把最新的 auto_calib 脚本 + 标定板拷进容器 /opt/auto_calib (幂等)。"""
    docker_exec(["mkdir", "-p", CONTAINER_SCRIPTS])
    subprocess.run(["docker", "cp", f"{SCRIPT_DIR}/.", f"{CONTAINER}:{CONTAINER_SCRIPTS}/"],
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if BOARD_DIR.is_dir():
        subprocess.run(["docker", "cp", f"{BOARD_DIR}/.", f"{CONTAINER}:{CONTAINER_SCRIPTS}/"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)


# --------------------------------------------------------------------------
# 列出可标定文件夹
# --------------------------------------------------------------------------
LIST_MARKER = "@@FOLDERS@@"


def list_folders() -> List[Dict[str, Any]]:
    """列出 /data 下含 calibration.bag 的文件夹, 标注 imu.bag / 已有 result*。"""
    script = (
        f'cd {DATA_ROOT} 2>/dev/null || exit 0; '
        'for d in */; do d="${d%/}"; '
        '[ -f "$d/calibration.bag" ] || continue; '
        'imu=0; [ -f "$d/imu.bag" ] && imu=1; '
        'res=""; for r in "$d"/result*; do [ -d "$r" ] && res="$res ${r#$d/}"; done; '
        'echo "'+LIST_MARKER+'$d|$imu|$res"; done'
    )
    p = docker_exec(["bash", "-lc", script])
    out: List[Dict[str, Any]] = []
    for line in p.stdout.splitlines():
        if not line.startswith(LIST_MARKER):
            continue
        body = line[len(LIST_MARKER):]
        parts = body.split("|")
        name = parts[0]
        has_imu = parts[1] == "1" if len(parts) > 1 else False
        results = parts[2].split() if len(parts) > 2 and parts[2].strip() else []
        out.append({"name": name, "has_imu": has_imu, "results": sorted(results)})
    out.sort(key=lambda x: x["name"])
    return out


# --------------------------------------------------------------------------
# 标定执行 (流式)
# --------------------------------------------------------------------------
def job_log(job: Job, text: str) -> None:
    """向 job 写一段服务端自己的说明文字 (与容器输出同一缓冲, 保证顺序与不被覆盖)。"""
    job.stdout_raw += text
    job.stdout = _normalize_cr(job.stdout_raw)


def run_one_folder(job: Job, folder: str) -> int:
    """docker exec 调 run_one.sh 标定单个文件夹, 流式写入 job。返回退出码。"""
    job.current = folder
    job_log(job, f"\n===== [标定开始] {folder} =====\n")
    env_args: List[str] = []
    for k, v in job.env.items():
        if k in ENV_KEYS and str(v).strip() != "":
            env_args += ["-e", f"{k}={v}"]
    cmd = (["docker", "exec"] + env_args + [CONTAINER, "bash",
           f"{CONTAINER_SCRIPTS}/run_one.sh", f"{DATA_ROOT}/{folder}"])
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            bufsize=0)   # 二进制管道: 保留 \r 进度刷新, 由 read_stream 解码
    job.process = proc
    q: "queue.Queue[tuple[str, str]]" = queue.Queue()
    threads = [
        threading.Thread(target=read_stream, args=("stdout", proc.stdout, q), daemon=True),
        threading.Thread(target=read_stream, args=("stderr", proc.stderr, q), daemon=True),
    ]
    for t in threads:
        t.start()
    killed = False
    while proc.poll() is None or any(t.is_alive() for t in threads):
        if job.cancel_requested and proc.poll() is None and not killed:
            proc.terminate()
            killed = True
        drain_output(job, q)
        time.sleep(0.1)
    drain_output(job, q)
    job.process = None
    rc = int(proc.returncode or 0)
    job.results[folder] = "ok" if rc == 0 else "fail"
    job_log(job, f"===== [标定结束] {folder} (退出码 {rc}) =====\n")
    return rc


def run_job(server: CalibServer, job: Job) -> None:
    job.status = "running"
    try:
        job_log(job, "[准备] 同步脚本到容器 & 检查 imu_utils / PyAV ...\n")
        sync_scripts_into_container()
        # 幂等确保 imu_utils / PyAV 就绪 (与 run_all.sh 一致)
        docker_exec(["bash", f"{CONTAINER_SCRIPTS}/setup_imu_utils.sh"])
        docker_exec(["bash", "-lc", 'python3 -c "import av" 2>/dev/null || pip install av'])
        job_log(job, "[准备] 完成, 开始标定\n")
        any_fail = False
        for folder in job.folders:
            if job.cancel_requested:
                job_log(job, "[已取消] 跳过剩余文件夹\n")
                break
            rc = run_one_folder(job, folder)
            if rc != 0:
                any_fail = True
        job.current = ""
        if job.cancel_requested:
            job.status = "canceled"
        else:
            job.status = "failed" if any_fail else "passed"
    except Exception as exc:  # noqa: BLE001
        job.status = "failed"
        job.error = str(exc)
        job_log(job, f"[server error] {exc}\n")
    finally:
        job.finished_at = time.time()


# --------------------------------------------------------------------------
# 结果解析 (YAML -> JSON + 位姿数学)
# --------------------------------------------------------------------------
PARSE_MARKER = "@@RESULT_JSON@@"

# 在容器内用 python 解析 yaml 并 emit 一行 JSON, 避免宿主机依赖 pyyaml。
_PARSE_SNIPPET = r'''
import json, sys, os, glob
try:
    import yaml
except Exception:
    print("''' + PARSE_MARKER + r'''" + json.dumps({"error":"no pyyaml in container"})); sys.exit(0)
rd = sys.argv[1]
out = {"cameras": [], "imu": None, "summary_text": "", "files": []}
def load(p):
    try:
        with open(p) as f: return yaml.safe_load(f)
    except Exception: return None
cc = load(os.path.join(rd, "''' + RESULT_YAML_CAMCHAIN + r'''"))
if cc:
    for k in sorted(cc.keys()):
        if not k.startswith("cam"): continue
        c = cc[k]
        out["cameras"].append({
            "name": k,
            "camera_model": c.get("camera_model"),
            "distortion_model": c.get("distortion_model"),
            "intrinsics": c.get("intrinsics"),
            "distortion_coeffs": c.get("distortion_coeffs"),
            "resolution": c.get("resolution"),
            "rostopic": c.get("rostopic"),
            "T_cn_cnm1": c.get("T_cn_cnm1"),
        })
ic = load(os.path.join(rd, "''' + RESULT_YAML_IMUCAM + r'''"))
if ic:
    c0 = ic.get("cam0", {})
    out["imu"] = {
        "T_cam_imu": c0.get("T_cam_imu"),
        "timeshift_cam_imu": c0.get("timeshift_cam_imu"),
    }
    imu0 = ic.get("imu0")
    if imu0: out["imu"]["params"] = imu0
sp = os.path.join(rd, "summary.txt")
if os.path.isfile(sp):
    with open(sp) as f: out["summary_text"] = f.read()
for p in sorted(glob.glob(os.path.join(rd, "*"))):
    if os.path.isfile(p): out["files"].append(os.path.basename(p))
print("''' + PARSE_MARKER + r'''" + json.dumps(out))
'''


def mat_mul(a, b):
    n, m, p = len(a), len(b), len(b[0])
    return [[sum(a[i][k] * b[k][j] for k in range(m)) for j in range(p)] for i in range(n)]


def rigid_inverse(T):
    """4x4 刚体变换求逆: [R t; 0 1]^-1 = [R^T -R^T t; 0 1]。"""
    R = [[T[i][j] for j in range(3)] for i in range(3)]
    t = [T[i][3] for i in range(3)]
    Rt = [[R[j][i] for j in range(3)] for i in range(3)]
    nt = [-(Rt[i][0]*t[0] + Rt[i][1]*t[1] + Rt[i][2]*t[2]) for i in range(3)]
    return [
        [Rt[0][0], Rt[0][1], Rt[0][2], nt[0]],
        [Rt[1][0], Rt[1][1], Rt[1][2], nt[1]],
        [Rt[2][0], Rt[2][1], Rt[2][2], nt[2]],
        [0.0, 0.0, 0.0, 1.0],
    ]


IDENTITY4 = [[1.0 if i == j else 0.0 for j in range(4)] for i in range(4)]


def compute_poses(cameras: List[Dict[str, Any]], imu: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """由 camchain 链式 T_cn_cnm1 累乘, 得到各 cam 相对 cam0 的绝对位姿 T_world_camN。

    T_cn_cnm1 = T_camN_cam(N-1)。相对 cam0: T_cam0_camN = T_cam0_cam(N-1) * inv(T_cn_cnm1)。
    IMU: imucam.yaml 的 cam0 含 T_cam_imu(=T_cam0_imu); IMU 在 cam0 坐标下的位姿 = inv(T_cam_imu)。
    """
    poses = []
    T_prev = IDENTITY4
    for idx, c in enumerate(cameras):
        if idx == 0 or not c.get("T_cn_cnm1"):
            T_world = IDENTITY4 if idx == 0 else T_prev
        else:
            T_world = mat_mul(T_prev, rigid_inverse(c["T_cn_cnm1"]))
        T_prev = T_world
        poses.append({"name": c["name"], "T": T_world})
    imu_pose = None
    if imu and imu.get("T_cam_imu"):
        imu_pose = {"name": "imu0", "T": rigid_inverse(imu["T_cam_imu"])}
    return {"cameras": poses, "imu": imu_pose}


def parse_summary_metrics(text: str) -> Dict[str, Any]:
    verdict = ""
    for line in text.splitlines():
        s = line.strip()
        if "总判定" in s or "判定" in s:
            if "FAIL" in s.upper():
                verdict = "FAIL"
            elif "WARN" in s.upper():
                verdict = "WARN"
            elif "OK" in s.upper() or "通过" in s:
                verdict = "OK"
    return {"verdict": verdict}


def get_result(folder: str, which: str = "") -> Dict[str, Any]:
    """解析某文件夹的标定结果; which 为 result 子目录名, 空则取最新。"""
    if which:
        rd = f"{DATA_ROOT}/{folder}/{which}"
    else:
        # 取最新 result* (按名字排序末尾)
        pick = (
            f'ls -d {DATA_ROOT}/{folder}/result* 2>/dev/null | sort | tail -1'
        )
        rp = docker_exec(["bash", "-lc", pick])
        rd = rp.stdout.strip()
        if not rd:
            return {"error": "该文件夹暂无 result* 结果目录"}
    which_name = rd.rstrip("/").split("/")[-1]
    p = docker_exec(["python3", "-c", _PARSE_SNIPPET, rd])
    data = None
    for line in reversed(p.stdout.splitlines()):
        if line.startswith(PARSE_MARKER):
            data = json.loads(line[len(PARSE_MARKER):])
            break
    if data is None:
        return {"error": "解析失败", "raw": (p.stdout + p.stderr)[-2000:]}
    if data.get("error"):
        return data
    data["folder"] = folder
    data["which"] = which_name
    data["poses"] = compute_poses(data.get("cameras", []), data.get("imu"))
    data["summary"] = parse_summary_metrics(data.get("summary_text", ""))
    return data


# --------------------------------------------------------------------------
# HTTP Handler
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "AutoCalibWeb/1.0"

    def log_message(self, fmt, *args):  # 静默默认日志
        pass

    # ---- helpers ----
    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data: bytes, content_type: str, status: int = 200,
                    disposition: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, name: str) -> None:
        # 只服务 web/ 目录下的文件, 防目录穿越
        normalized = posixpath.normpath("/" + name).lstrip("/")
        if normalized.startswith("../") or ".." in normalized.split("/"):
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        path = WEB_DIR / normalized
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8" if "charset" not in ctype else ""
        self._send_bytes(path.read_bytes(), ctype)

    def _read_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---- GET ----
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                self._serve_static("index.html")
            elif path == "/viz.html":
                self._serve_static("viz.html")
            elif path.startswith("/static/"):
                self._serve_static(path[len("/static/"):])
            elif path == "/api/config":
                self._send_json({"container": CONTAINER, "data_root": DATA_ROOT,
                                 "running": container_running()})
            elif path == "/api/folders":
                if not container_running():
                    self._send_json({"error": f"容器 {CONTAINER} 未运行", "folders": []})
                    return
                self._send_json({"folders": list_folders()})
            elif path == "/api/jobs":
                self._send_json({"jobs": [j.to_dict() for j in self.server.list_jobs()]})
            elif path.startswith("/api/jobs/"):
                job_id = path[len("/api/jobs/"):]
                job = self.server.get_job(job_id)
                if job is None:
                    self._send_json({"error": "job not found"}, 404)
                else:
                    self._send_json(job.to_dict())
            elif path == "/api/result":
                folder = (qs.get("folder") or [""])[0]
                which = (qs.get("which") or [""])[0]
                if not folder:
                    self._send_json({"error": "missing folder"}, 400)
                    return
                self._send_json(get_result(folder, which))
            elif path == "/api/file":
                self._serve_result_file(qs)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": str(exc)}, 500)

    def _serve_result_file(self, qs: Dict[str, List[str]]) -> None:
        folder = (qs.get("folder") or [""])[0]
        which = (qs.get("which") or [""])[0]
        name = (qs.get("name") or [""])[0]
        if not folder or not which or not name:
            self._send_json({"error": "missing folder/which/name"}, 400)
            return
        # 校验各段, 禁止穿越
        for seg in (folder, which, name):
            if "/" in seg or ".." in seg or seg.strip() == "":
                self._send_json({"error": "invalid path"}, 400)
                return
        remote = f"{DATA_ROOT}/{folder}/{which}/{name}"
        # 用 docker cp 到临时文件再回传
        tmp = f"/tmp/autocalib_dl_{uuid.uuid4().hex[:8]}_{name}"
        cp = subprocess.run(["docker", "cp", f"{CONTAINER}:{remote}", tmp],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if cp.returncode != 0 or not os.path.isfile(tmp):
            self._send_json({"error": "file not found", "detail": cp.stderr[-500:]}, 404)
            return
        try:
            data = Path(tmp).read_bytes()
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        inline = name.lower().endswith((".pdf", ".png", ".jpg", ".jpeg", ".txt", ".yaml", ".yml", ".log"))
        disp = f'{"inline" if inline else "attachment"}; filename="{name}"'
        if ctype.startswith("text/") or name.lower().endswith((".yaml", ".yml", ".log", ".txt")):
            ctype = "text/plain; charset=utf-8"
        self._send_bytes(data, ctype, disposition=disp)

    # ---- POST ----
    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/jobs":
                self._create_job()
            elif path.startswith("/api/jobs/") and path.endswith("/cancel"):
                job_id = path[len("/api/jobs/"):-len("/cancel")]
                job = self.server.get_job(job_id)
                if job is None:
                    self._send_json({"error": "job not found"}, 404)
                    return
                job.cancel_requested = True
                if job.process is not None:
                    try:
                        job.process.terminate()
                    except Exception:
                        pass
                self._send_json({"ok": True})
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": str(exc)}, 500)

    def _create_job(self) -> None:
        if not container_running():
            self._send_json({"error": f"容器 {CONTAINER} 未运行, 请先 docker start {CONTAINER}"}, 400)
            return
        body = self._read_body()
        folders = body.get("folders") or []
        if isinstance(folders, str):
            folders = [folders]
        folders = [f for f in folders if isinstance(f, str) and f.strip()
                   and "/" not in f and ".." not in f]
        if not folders:
            self._send_json({"error": "未选择有效文件夹"}, 400)
            return
        env = {}
        for k, v in (body.get("env") or {}).items():
            if k in ENV_KEYS and str(v).strip() != "":
                env[k] = str(v)
        job = Job(job_id=uuid.uuid4().hex[:12], folders=folders, env=env)
        self.server.add_job(job)
        threading.Thread(target=run_job, args=(self.server, job), daemon=True).start()
        self._send_json(job.to_dict())


def main() -> None:
    global CONTAINER, DATA_ROOT
    parser = argparse.ArgumentParser(description="auto_calib 批量标定 Web 服务")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--container", default=CONTAINER, help="复用的长驻容器名 (默认 kalibr_work)")
    parser.add_argument("--data-root", default=DATA_ROOT, help="容器内数据根 (默认 /data)")
    args = parser.parse_args()
    CONTAINER = args.container
    DATA_ROOT = args.data_root

    mimetypes.add_type("application/javascript", ".js")
    server = CalibServer((args.host, args.port), Handler)
    print(f"auto_calib Web 服务: http://{args.host}:{args.port}", flush=True)
    print(f"  复用容器: {CONTAINER}   数据根: {DATA_ROOT}", flush=True)
    if not container_running():
        print(f"  [警告] 容器 {CONTAINER} 当前未运行, 标定前请先启动它", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止服务", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
