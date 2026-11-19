
import os
import re
import io
import json
import time
import signal
import zipfile
import tempfile
import subprocess
import streamlit as st

from google import genai
import imageio_ffmpeg


st.set_page_config(
    page_title="AI Live Clipper",
    page_icon="🎬",
    layout="centered"
)

st.title("🎬 AI YouTube Live Clipper")
st.write("LIVE stream record karo aur AI se Shorts banao.")

# API KEY
api_key = st.secrets.get("GEMINI_API_KEY", "")

if not api_key:
    st.error("Gemini API Key missing hai. Cloud Secrets mein add karo.")
    st.stop()

client = genai.Client(api_key=api_key)
ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()


# -------------------------
# HELPER FUNCTIONS
# -------------------------

def get_video_files(folder):
    files = []

    for name in os.listdir(folder):
        path = os.path.join(folder, name)

        if name.startswith("capture.") and os.path.isfile(path):
            if not name.endswith((".part", ".ytdl")):
                if os.path.getsize(path) > 100000:
                    files.append(path)

    return files


def record_live(url, folder, seconds, status):
    output = os.path.join(folder, "capture.%(ext)s")
    log_path = os.path.join(folder, "recording.log")

    command = [
    "yt-dlp",
    "--remote-components",
    "ejs:npm",
        "--no-playlist",
        "--no-part",
        "--newline",
        "-f",
"best[height<=480]/best",
        "--merge-output-format",
        "mp4",
        "-o",
        output,
        url
    ]

    start = time.time()

    with open(log_path, "w") as log:
        process = subprocess.Popen(
            command,
            stdout=log,
            stderr=subprocess.STDOUT
        )

        try:
            while time.time() - start < seconds:
                if process.poll() is not None:
                    break

                elapsed = int(time.time() - start)
                remaining = max(0, seconds - elapsed)

                hours = elapsed // 3600
                minutes = (elapsed % 3600) // 60
                secs = elapsed % 60

                status.update(
                    label=(
                        f"Recording LIVE: "
                        f"{hours:02d}:{minutes:02d}:{secs:02d}"
                    ),
                    state="running"
                )

                time.sleep(5)

        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)

                try:
                    process.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    process.terminate()

                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()

    files = get_video_files(folder)

    if not files:
        with open(log_path, "r") as f:
            error = f.read()

        raise Exception(
            "LIVE recording file nahi mili.\n" + error[-4000:]
        )

    video = max(files, key=os.path.getsize)

    if os.path.getsize(video) < 100000:
        raise Exception("Recording file bahut chhoti ya incomplete hai.")

    return video


def download_normal(url, folder):
    output = os.path.join(folder, "capture.%(ext)s")

    command = [
        "yt-dlp",
        "--no-playlist",
        "-f",
        "best[height<=480][ext=mp4]/best[height<=480]/best",
        "--merge-output-format",
        "mp4",
        "-o",
        output,
        url
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=900
    )

    if result.returncode != 0:
        raise Exception(result.stderr[-4000:])

    files = get_video_files(folder)

    if not files:
        raise Exception("Video download nahi hui.")

    return max(files, key=os.path.getsize)


def get_duration(video):
    result = subprocess.run(
        [ffmpeg, "-i", video],
        capture_output=True,
        text=True
    )

    text = result.stderr

    match = re.search(
        r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)",
        text
    )

    if not match:
        raise Exception("Video ki duration read nahi ho payi.")

    h, m, s = match.groups()

    return int(float(h) * 3600 + int(m) * 60 + float(s))


def make_chunks(video, folder, duration):
    chunk_length = 600
    chunks = []

    total_chunks = (duration + chunk_length - 1) // chunk_length

    for index in range(total_chunks):
        start = index * chunk_length
        length = min(chunk_length, duration - start)

        output = os.path.join(
            folder,
            f"analysis_{index + 1}.mp4"
        )

        command = [
            ffmpeg,
            "-y",
            "-ss", str(start),
            "-i", video,
            "-t", str(length),
            "-map", "0:v:0",
            "-map", "0:a?",
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            output
        ]

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=300
        )

        if result.returncode != 0 or not os.path.exists(output):
            raise Exception(
                "Video chunk nahi ban paya:\n" +
                result.stderr[-3000:]
            )

        if os.path.getsize(output) > 100000:
            chunks.append((output, start, length))

    return chunks


def analyze_chunk(chunk_path, offset, length, clip_seconds):
    uploaded = client.files.upload(file=chunk_path)

    for _ in range(120):
        uploaded = client.files.get(name=uploaded.name)

        if uploaded.state and uploaded.state.name == "ACTIVE":
            break

        if uploaded.state and uploaded.state.name == "FAILED":
            raise Exception("Gemini video processing failed.")

        time.sleep(5)
    else:
        raise Exception("Gemini video processing timeout.")

    prompt = f"""
    Analyze this video carefully and find interesting moments
    suitable for viral YouTube Shorts.

    Return ONLY valid JSON in this format:

    {{
      "clips": [
        {{
          "title": "Short title",
          "start_sec": 20,
          "end_sec": 50,
          "score": 9
        }}
      ]
    }}

    RULES:
    - Each clip must be exactly about {clip_seconds} seconds.
    - Find up to 3 good moments in this chunk.
    - start_sec and end_sec must be relative to this chunk.
    - Never go outside this chunk's duration of {length} seconds.
    - Do not invent events.
    - Score each clip from 1 to 10.
    - Return valid JSON only.
    """

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[uploaded, prompt]
    )

    text = response.text or ""

    match = re.search(
        r"\{.*\}",
        text,
        re.DOTALL
    )

    if not match:
        return []

    data = json.loads(match.group(0))
    candidates = []

    for item in data.get("clips", []):
        try:
            start = float(item["start_sec"])
            end = float(item["end_sec"])

            if (
                start >= 0
                and end > start
                and end - start <= clip_seconds + 5
                and end <= length
            ):
                candidates.append({
                    "title": str(item.get("title", "AI Short")),
                    "start": offset + start,
                    "end": offset + end,
                    "score": float(item.get("score", 5))
                })

        except (ValueError, TypeError, KeyError):
            continue

    return candidates


def create_clip(video, start, end, output):
    command = [
        ffmpeg,
        "-y",
        "-ss", str(start),
        "-i", video,
        "-t", str(end - start),
        "-vf",
        "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "25",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        output
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=600
    )

    if result.returncode != 0:
        raise Exception(result.stderr[-3000:])


# -------------------------
# APP OPTIONS
# -------------------------

url = st.text_input(
    "YouTube LIVE / Video Link",
    placeholder="https://www.youtube.com/watch?v=..."
)

record_choice = st.selectbox(
    "Kitni der recording karni hai?",
    [
        "30 minutes",
        "1 hour",
        "2 hours",
        "3 hours",
        "4 hours",
        "Custom"
    ]
)

record_times = {
    "30 minutes": 30,
    "1 hour": 60,
    "2 hours": 120,
    "3 hours": 180,
    "4 hours": 240
}

if record_choice == "Custom":
    record_minutes = st.number_input(
        "Recording minutes",
        min_value=1,
        max_value=240,
        value=60
    )
else:
    record_minutes = record_times[record_choice]

clip_count_choice = st.selectbox(
    "Kitni clips banani hain?",
    ["5", "10", "20", "Custom"]
)

if clip_count_choice == "Custom":
    clip_count = st.number_input(
        "Clips ki quantity",
        min_value=1,
        max_value=50,
        value=5
    )
else:
    clip_count = int(clip_count_choice)

clip_length_choice = st.selectbox(
    "Har clip kitne seconds ki ho?",
    ["30 seconds", "45 seconds", "60 seconds", "Custom"]
)

if clip_length_choice == "Custom":
    clip_seconds = st.number_input(
        "Custom clip seconds",
        min_value=10,
        max_value=180,
        value=30
    )
else:
    clip_seconds = int(clip_length_choice.split()[0])


st.info(
    "LIVE recording current point se start hogi. "
    "Recording khatam hone ke baad AI clips banayega."
)


# -------------------------
# START BUTTON
# -------------------------

if st.button("🚀 Start Recording & Generate Clips", type="primary"):

    if not url.startswith((
        "https://www.youtube.com/",
        "https://youtu.be/",
        "https://youtube.com/"
    )):
        st.error("Valid YouTube link enter karo.")

    else:
        try:
            with tempfile.TemporaryDirectory() as temp:

                status = st.status(
                    "LIVE recording start ho rahi hai...",
                    expanded=True
                )

                st.write(
                    f"Recording time: {record_minutes} minutes"
                )

                video = record_live(
                    url,
                    temp,
                    int(record_minutes * 60),
                    status
                )

                status.update(
                    label="Recording complete!",
                    state="complete"
                )

                st.success("LIVE recording save ho gayi.")

                duration = get_duration(video)

                st.write(
                    f"Recorded video duration: "
                    f"{duration // 3600}h "
                    f"{(duration % 3600) // 60}m"
                )

                st.write("Video ko 10-minute parts mein divide kar rahe hain...")

                chunks = make_chunks(
                    video,
                    temp,
                    duration
                )

                if not chunks:
                    raise Exception("Analysis ke liye video parts nahi mile.")

                candidates = []

                progress = st.progress(0)
                message = st.empty()

                for index, (chunk, offset, length) in enumerate(chunks):

                    message.write(
                        f"AI analysis: Part {index + 1} / {len(chunks)}"
                    )

                    found = analyze_chunk(
                        chunk,
                        offset,
                        length,
                        int(clip_seconds)
                    )

                    candidates.extend(found)
                    progress.progress((index + 1) / len(chunks))

                    os.remove(chunk)

                # Highest scoring candidates first
                candidates.sort(
                    key=lambda x: x["score"],
                    reverse=True
                )

                selected = []

                for item in candidates:
                    overlaps = False

                    for old in selected:
                        if (
                            item["start"] < old["end"]
                            and item["end"] > old["start"]
                        ):
                            overlaps = True
                            break

                    if not overlaps:
                        selected.append(item)

                    if len(selected) >= int(clip_count):
                        break

                if not selected:
                    raise Exception("AI ko suitable clips nahi mile.")

                st.success(
                    f"{len(selected)} clips select hui hain."
                )

                zip_buffer = io.BytesIO()

                with zipfile.ZipFile(
                    zip_buffer,
                    "w",
                    zipfile.ZIP_DEFLATED
                ) as zip_file:

                    for index, item in enumerate(selected, start=1):

                        output = os.path.join(
                            temp,
                            f"short_{index}.mp4"
                        )

                        st.write(
                            f"Creating clip {index}: {item['title']}"
                        )

                        create_clip(
                            video,
                            item["start"],
                            item["end"],
                            output
                        )

                        with open(output, "rb") as f:
                            clip_data = f.read()

                        zip_file.writestr(
                            f"clip_{index}.mp4",
                            clip_data
                        )

                        st.subheader(
                            f"Clip {index}: {item['title']}"
                        )

                        st.video(clip_data)

                        st.download_button(
                            f"⬇️ Download Clip {index}",
                            data=clip_data,
                            file_name=f"short_{index}.mp4",
                            mime="video/mp4",
                            key=f"download_{index}"
                        )

                st.download_button(
                    "📦 Download All Clips (ZIP)",
                    data=zip_buffer.getvalue(),
                    file_name="AI_YouTube_Clips.zip",
                    mime="application/zip"
                )

        except subprocess.TimeoutExpired:
            st.error("Process timeout ho gaya. Chhoti recording try karo.")

        except Exception as e:
            st.error("Something went wrong:")
            st.code(str(e)[-5000:])
