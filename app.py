
import streamlit as st
import os
import re
import json
import time
import signal
import shutil
import zipfile
import tempfile
import subprocess
import urllib.request

from pathlib import Path
from google import genai
from google.genai import types
import imageio_ffmpeg


# ----------------------------
# APP SETTINGS
# ----------------------------

st.set_page_config(
    page_title="AI YouTube Clipper",
    page_icon="🎬",
    layout="wide"
)

st.title("🎬 AI YouTube Clipper")
st.write("YouTube video ya LIVE record karo aur AI se Shorts banao.")

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
WORK_DIR = Path(tempfile.gettempdir()) / "ai_clipper"
WORK_DIR.mkdir(exist_ok=True)


# ----------------------------
# DENO INSTALLER
# ----------------------------

@st.cache_resource(show_spinner=False)
def install_deno():
    deno_folder = Path(tempfile.gettempdir()) / "deno_runtime"
    deno_path = deno_folder / "deno"

    if deno_path.exists():
        return str(deno_path)

    deno_folder.mkdir(parents=True, exist_ok=True)

    zip_path = deno_folder / "deno.zip"

    url = (
        "https://github.com/denoland/deno/releases/"
        "latest/download/deno-x86_64-unknown-linux-gnu.zip"
    )

    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "AI-YouTube-Clipper"}
        )

        with urllib.request.urlopen(request, timeout=120) as response:
            zip_path.write_bytes(response.read())

        with zipfile.ZipFile(zip_path, "r") as archive:
            archive.extract("deno", deno_folder)

        deno_path.chmod(0o755)
        zip_path.unlink(missing_ok=True)

        return str(deno_path)

    except Exception as e:
        raise RuntimeError(
            "Deno install nahi ho paya. Internet ya GitHub download "
            "block ho sakta hai. Error: " + str(e)
        )


# ----------------------------
# YOUTUBE DOWNLOAD
# ----------------------------

def get_ytdlp_command():
    deno = install_deno()

    return [
        "yt-dlp",
        "--js-runtimes",
        f"deno:{deno}",
        "--remote-components",
        "ejs:npm"
    ]


def download_normal(url, output_folder):
    output_template = str(output_folder / "video.%(ext)s")

    command = get_ytdlp_command() + [
        "--no-playlist",
        "--no-warnings",
        "-f", "best",
        "--merge-output-format", "mp4",
        "-o", output_template,
        url
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=3600
    )

    if result.returncode != 0:
        raise RuntimeError(
            result.stderr[-4000:] or "YouTube download failed."
        )

    files = list(output_folder.glob("video.*"))
    files = [f for f in files if f.is_file() and f.suffix != ".part"]

    if not files:
        raise RuntimeError("Downloaded video file nahi mili.")

    return files[0]


def record_live(url, minutes, output_folder):
    output_file = output_folder / "live_recording.%(ext)s"

    command = get_ytdlp_command() + [
        "--no-playlist",
        "--no-part",
        "--no-warnings",
        "-f", "best",
        "-o", str(output_file),
        url
    ]

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    log_lines = []
    start_time = time.time()
    duration_seconds = minutes * 60

    try:
        while time.time() - start_time < duration_seconds:
            line = process.stdout.readline()

            if line:
                log_lines.append(line.strip())
                if len(log_lines) > 30:
                    log_lines.pop(0)

            if process.poll() is not None:
                break

            time.sleep(0.2)

    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)

            try:
                process.wait(timeout=40)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    files = list(output_folder.glob("live_recording.*"))
    files = [
        f for f in files
        if f.is_file()
        and f.suffix not in [".part", ".ytdl"]
    ]

    if not files:
        raise RuntimeError(
            "LIVE recording file nahi bani.\n\n"
            + "\n".join(log_lines[-15:])
        )

    return files[0]


# ----------------------------
# VIDEO INFORMATION
# ----------------------------

def get_duration(video_path):
    command = [
        FFMPEG.replace("ffmpeg", "ffprobe"),
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path)
    ]

    # imageio-ffmpeg may not provide ffprobe, so use ffmpeg fallback
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode == 0:
            return float(result.stdout.strip())

    except Exception:
        pass

    return None


# ----------------------------
# SPLIT VIDEO INTO CHUNKS
# ----------------------------

def make_chunks(video_path, folder, chunk_minutes=10):
    chunk_seconds = chunk_minutes * 60
    chunks = []

    duration = get_duration(video_path)

    if not duration:
        raise RuntimeError(
            "Video duration read nahi ho payi. FFmpeg error ho sakta hai."
        )

    start = 0
    number = 1

    while start < duration:
        output = folder / f"chunk_{number}.mp4"

        command = [
            FFMPEG,
            "-y",
            "-ss", str(start),
            "-i", str(video_path),
            "-t", str(chunk_seconds),
            "-c", "copy",
            str(output)
        ]

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=900
        )

        if result.returncode == 0 and output.exists():
            chunks.append((output, start))

        start += chunk_seconds
        number += 1

    if not chunks:
        raise RuntimeError("Video chunks nahi ban paye.")

    return chunks


# ----------------------------
# GEMINI ANALYSIS
# ----------------------------

def get_gemini_client():
    api_key = st.secrets.get("GEMINI_API_KEY", "")

    if not api_key:
        api_key = os.environ.get("GEMINI_API_KEY", "")

    if not api_key:
        raise RuntimeError(
            "Gemini API key nahi mili. Streamlit Secrets mein "
            "GEMINI_API_KEY add karo."
        )

    return genai.Client(api_key=api_key)


def analyze_chunk(client, chunk_path, offset, clip_count, clip_seconds):
    uploaded = client.files.upload(file=str(chunk_path))

    try:
        while uploaded.state.name == "PROCESSING":
            time.sleep(5)
            uploaded = client.files.get(name=uploaded.name)

        if uploaded.state.name == "FAILED":
            raise RuntimeError("Gemini video process nahi kar paya.")

        prompt = f"""
        Analyze this video and find the {clip_count} most interesting,
        entertaining or important moments suitable for YouTube Shorts.

        Each clip should be approximately {clip_seconds} seconds long.

        Return ONLY valid JSON in this format:
        {{
          "clips": [
            {{
              "start": 10,
              "end": 40,
              "title": "Interesting moment",
              "reason": "Why this moment is worth watching"
            }}
          ]
        }}

        Rules:
        - Start and end must be seconds inside THIS uploaded video.
        - End must be greater than start.
        - Do not invent moments.
        - Avoid duplicate moments.
        - Keep clips within the video duration.
        - Prefer moments with a clear beginning and ending.
        """

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[uploaded, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )

        data = json.loads(response.text)

        clips = []

        for clip in data.get("clips", []):
            start = float(clip["start"]) + offset
            end = float(clip["end"]) + offset

            if end > start:
                clips.append({
                    "start": start,
                    "end": end,
                    "title": str(clip.get("title", "AI Clip")),
                    "reason": str(clip.get("reason", ""))
                })

        return clips

    finally:
        try:
            client.files.delete(name=uploaded.name)
        except Exception:
            pass


# ----------------------------
# CREATE VERTICAL SHORTS
# ----------------------------

def create_clip(video_path, clip, output_path):
    start = clip["start"]
    duration = clip["end"] - clip["start"]

    command = [
        FFMPEG,
        "-y",
        "-ss", str(start),
        "-i", str(video_path),
        "-t", str(duration),
        "-vf",
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "24",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(output_path)
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=900
    )

    if result.returncode != 0:
        raise RuntimeError(result.stderr[-2000:])

    return output_path


# ----------------------------
# USER INTERFACE
# ----------------------------

st.subheader("1. Video source")

source = st.radio(
    "Video kaise dena hai?",
    ["YouTube video link", "YouTube LIVE recording", "Upload video"],
    horizontal=False
)

url = ""
uploaded_video = None

if source == "YouTube video link":
    url = st.text_input("YouTube video URL")

elif source == "YouTube LIVE recording":
    url = st.text_input("YouTube LIVE URL")

    recording_minutes = st.selectbox(
        "Kitni der record karna hai?",
        [30, 60, 120, 180, 240],
        format_func=lambda x: (
            f"{x} minutes" if x < 60 else f"{x // 60} hour"
            if x == 60 else f"{x // 60} hours"
        )
    )

else:
    uploaded_video = st.file_uploader(
        "Video upload karo",
        type=["mp4", "mov", "mkv", "webm"]
    )


st.subheader("2. Shorts settings")

clip_count = st.selectbox(
    "Kitne clips banane hain?",
    [5, 10, 20]
)

clip_seconds = st.selectbox(
    "Har clip kitne seconds ki ho?",
    [30, 45, 60]
)

st.caption(
    "Note: Gemini AI available moments ke hisaab se clips banaega. "
    "Har video mein exact requested number milna guaranteed nahi hai."
)


# ----------------------------
# PROCESS BUTTON
# ----------------------------

if st.button("🚀 Start AI Clipping", type="primary"):

    if source in ["YouTube video link", "YouTube LIVE recording"] and not url.strip():
        st.error("Pehle YouTube link paste karo.")

    elif source == "Upload video" and uploaded_video is None:
        st.error("Pehle video upload karo.")

    else:
        session_folder = Path(
            tempfile.mkdtemp(prefix="clipper_")
        )

        try:
            with st.spinner("Video prepare ho rahi hai..."):

                if source == "YouTube video link":
                    video_path = download_normal(
                        url.strip(),
                        session_folder
                    )

                elif source == "YouTube LIVE recording":
                    st.info(
                        "Recording abhi shuru hogi. App ko band mat karna."
                    )

                    video_path = record_live(
                        url.strip(),
                        recording_minutes,
                        session_folder
                    )

                else:
                    video_path = session_folder / uploaded_video.name

                    with open(video_path, "wb") as f:
                        f.write(uploaded_video.getbuffer())

            st.success("Video ready ho gayi!")

            st.video(str(video_path))

            with st.spinner("Video ko 10-minute parts mein divide kar rahe hain..."):
                chunks = make_chunks(
                    video_path,
                    session_folder
                )

            st.write(f"Total video parts: {len(chunks)}")

            client = get_gemini_client()
            all_clips = []

            progress = st.progress(0)

            for index, (chunk_path, offset) in enumerate(chunks):
                st.write(f"AI analysis: Part {index + 1}/{len(chunks)}")

                remaining = clip_count - len(all_clips)

                if remaining <= 0:
                    break

                clips = analyze_chunk(
                    client,
                    chunk_path,
                    offset,
                    remaining,
                    clip_seconds
                )

                all_clips.extend(clips)
                progress.progress((index + 1) / len(chunks))

            # Sort and remove overlapping clips
            all_clips.sort(key=lambda x: x["start"])

            selected = []
            for clip in all_clips:
                if len(selected) >= clip_count:
                    break

                if not selected or clip["start"] >= selected[-1]["end"]:
                    selected.append(clip)

            if not selected:
                raise RuntimeError(
                    "AI ko suitable clips nahi mile. Dusri video try karo."
                )

            st.subheader("3. Creating Shorts")

            clips_folder = session_folder / "shorts"
            clips_folder.mkdir(exist_ok=True)

            zip_path = session_folder / "shorts.zip"

            for index, clip in enumerate(selected, start=1):
                output = clips_folder / f"short_{index}.mp4"

                with st.spinner(f"Short {index} ban rahi hai..."):
                    create_clip(video_path, clip, output)

                st.markdown(f"**Short {index}: {clip['title']}**")
                st.caption(clip["reason"])
                st.video(str(output))

                with open(output, "rb") as file:
                    st.download_button(
                        f"⬇️ Download Short {index}",
                        data=file.read(),
                        file_name=f"short_{index}.mp4",
                        mime="video/mp4",
                        key=f"download_{index}"
                    )

            # ZIP creation
            with zipfile.ZipFile(
                zip_path,
                "w",
                zipfile.ZIP_DEFLATED
            ) as zip_file:

                for file in clips_folder.glob("*.mp4"):
                    zip_file.write(file, arcname=file.name)

            with open(zip_path, "rb") as file:
                st.download_button(
                    "📦 Download All Shorts (ZIP)",
                    data=file.read(),
                    file_name="youtube_shorts.zip",
                    mime="application/zip"
                )

            st.success(
                f"Done! {len(selected)} Shorts ready hain."
            )

        except Exception as e:
            st.error("Error aa gaya:")
            st.code(str(e))

            st.info(
                "Agar YouTube download fail ho, error ka screenshot "
                "bhejo. API key ya password screenshot mein mat dikhana."
            )
