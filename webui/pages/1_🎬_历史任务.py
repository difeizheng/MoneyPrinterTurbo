"""
历史任务列表页（Streamlit 传统 multipage）。

Main.py 保持为首页不动，本文件放在 webui/pages/ 下，Streamlit 会在侧边栏
自动生成「🎬 历史任务」入口。

数据来源：直接枚举 storage/tasks/ 磁盘目录（与 API 容器共享存储卷），
读取每个任务的 script.json 元数据。不依赖 API 的内存 state（重启会丢）。
"""

import glob
import json
import os
import shutil
import subprocess
import sys
import webbrowser
from datetime import datetime
from uuid import UUID

import streamlit as st
from loguru import logger

# 把项目根目录加入 sys.path，让 from app... 可用（与 Main.py 一致）
root_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if root_dir not in sys.path:
    sys.path.append(root_dir)

from app.config import config  # noqa: E402
from app.utils import utils  # noqa: E402

st.set_page_config(
    page_title="历史任务 - MoneyPrinterTurbo",
    page_icon="🎬",
    layout="wide",
)

i18n_dir = os.path.join(root_dir, "webui", "i18n")
locales = utils.load_locales(i18n_dir)

if "ui_language" not in st.session_state:
    st.session_state["ui_language"] = config.ui.get(
        "language", utils.get_system_locale()
    )


def tr(key: str) -> str:
    """自包含的 i18n 查询（与 Main.py 同逻辑，避免 import Main 触发整页渲染）。"""
    loc = locales.get(st.session_state.get("ui_language", ""), {})
    return loc.get("Translation", {}).get(key, key)


# 下载链接的 API 基址：本机默认 127.0.0.1:8080；远程访问时在 config 里设 api_url。
API_BASE = str(config.app.get("api_url", "http://127.0.0.1:8080")).rstrip("/")
TASKS_ROOT = os.path.abspath(utils.task_dir())


# ===================== 数据加载 =====================

@st.cache_data(ttl=60, show_spinner=False)
def load_tasks() -> list[dict]:
    """枚举所有历史任务。返回原始字段，不做 i18n（避免缓存依赖语言）。"""
    tasks: list[dict] = []
    if not os.path.isdir(TASKS_ROOT):
        return tasks

    for name in os.listdir(TASKS_ROOT):
        task_path = os.path.join(TASKS_ROOT, name)
        if not os.path.isdir(task_path):
            continue
        # 目录名必须是合法 UUID，过滤掉临时/异常目录
        try:
            task_id = str(UUID(name))
        except (ValueError, AttributeError, TypeError):
            continue

        script_file = os.path.join(task_path, "script.json")
        if not os.path.isfile(script_file):
            continue

        try:
            with open(script_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        params = data.get("params", {}) or {}
        final_videos = sorted(glob.glob(os.path.join(task_path, "final-*.mp4")))
        tasks.append(
            {
                "task_id": task_id,
                "subject": (params.get("video_subject") or "").strip(),
                "voice": params.get("voice_name") or "-",
                "aspect": params.get("video_aspect") or "-",
                "created_at": os.path.getmtime(script_file),
                "final_video": final_videos[-1] if final_videos else None,
                "completed": bool(final_videos),
            }
        )

    # 按创建时间倒序（最新的在最前）
    tasks.sort(key=lambda t: t["created_at"], reverse=True)
    return tasks


def format_time(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


def get_video_duration(path: str) -> str:
    """用 ffprobe 取时长，失败则返回 '-'。仅对选中任务调用一次。"""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        seconds = float(result.stdout.strip())
        minutes, secs = divmod(int(seconds), 60)
        return f"{minutes}分{secs}秒"
    except Exception:
        return "-"


def open_task_folder(task_id: str) -> None:
    """打开任务目录（UUID 校验 + 路径穿越防护，逻辑同 Main.py:142）。"""
    try:
        normalized = str(UUID(str(task_id)))
        path = os.path.abspath(os.path.join(TASKS_ROOT, normalized))
        if not path.startswith(TASKS_ROOT + os.sep):
            logger.warning(f"invalid task folder path: {path}")
            return
        if os.path.isdir(path):
            webbrowser.open(f"file://{path}")
    except Exception as e:
        logger.error(e)


# ===================== 顶部：标题 + 语言选择 =====================

title_col, lang_col = st.columns([3, 1])

with title_col:
    st.title(f"🎬 {tr('History Tasks')}")

with lang_col:
    display_languages: list[str] = []
    selected_index = 0
    for i, code in enumerate(locales.keys()):
        display_languages.append(f"{code} - {locales[code].get('Language')}")
        if code == st.session_state.get("ui_language", ""):
            selected_index = i
    selected_language = st.selectbox(
        "Language / 语言",
        options=display_languages,
        index=selected_index,
        key="history_language_selector",
        label_visibility="collapsed",
    )
    if selected_language:
        code = selected_language.split(" - ")[0].strip()
        st.session_state["ui_language"] = code

# ===================== 刷新按钮 + 数量 =====================

top_col1, top_col2 = st.columns([1, 4])
with top_col1:
    if st.button("🔄 " + tr("Refresh"), use_container_width=True):
        st.cache_data.clear()
        st.rerun()

tasks = load_tasks()
with top_col2:
    st.caption(f"📜 {tr('History Tasks')}: {len(tasks)}")

if not tasks:
    st.info(tr("No Tasks Found"))
    st.stop()

# ===================== 选择任务 =====================


def task_label(idx: int) -> str:
    t = tasks[idx]
    subject = t["subject"][:30] if t["subject"] else tr("No Subject")
    status = "✅" if t["completed"] else "⚠️"
    return f"{status} {subject} ({format_time(t['created_at'])})"


selected_index = st.selectbox(
    tr("Select Task"),
    options=range(len(tasks)),
    format_func=task_label,
)
task = tasks[selected_index]

# ===================== 详情面板 =====================

st.divider()
st.subheader(task["subject"] or tr("No Subject"))

# 元数据
m1, m2, m3, m4 = st.columns(4)
m1.metric(tr("Created At"), format_time(task["created_at"]))
m2.metric(
    tr("Status"),
    tr("Completed") if task["completed"] else tr("Incomplete"),
)
m3.metric(tr("Voice"), task["voice"])
m4.metric(tr("Aspect"), task["aspect"])

# 视频预览
if task["final_video"]:
    duration = get_video_duration(task["final_video"])
    st.caption(f"⏱️ {tr('Duration')}: {duration}")
    # 相对项目根的路径，宿主机和容器里都能据此定位
    rel_path = os.path.relpath(task["final_video"], root_dir).replace("\\", "/")
    st.caption(f"📁 {rel_path}")
    st.video(task["final_video"])
else:
    st.warning(tr("Incomplete"))

# ===================== 操作按钮 =====================

st.write("")
action_cols = st.columns([1, 1, 1, 2])
confirm_key = f"delete_confirm_{task['task_id']}"

with action_cols[0]:
    if st.button("📂 " + tr("Open Folder"), use_container_width=True):
        open_task_folder(task["task_id"])

with action_cols[1]:
    if task["final_video"]:
        fname = os.path.basename(task["final_video"])
        url = f"{API_BASE}/tasks/{task['task_id']}/{fname}"
        st.markdown(f"[⬇️ {tr('Download')}]({url})")
    else:
        st.write("")

with action_cols[2]:
    armed = st.session_state.get(confirm_key, False)
    btn_label = "⚠️ " + tr("Confirm Delete") if armed else "🗑️ " + tr("Delete")
    if st.button(
        btn_label,
        use_container_width=True,
        type="primary" if armed else "secondary",
    ):
        if armed:
            try:
                shutil.rmtree(os.path.join(TASKS_ROOT, task["task_id"]))
                st.session_state[confirm_key] = False
                st.cache_data.clear()
                st.success(tr("Deleted"))
                st.rerun()
            except Exception as e:
                st.error(f"{e}")
        else:
            st.session_state[confirm_key] = True
            st.rerun()
