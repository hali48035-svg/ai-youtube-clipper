
import os
import re
import json
import time
import tempfile
import subprocess
import streamlit as st

from google import genai
import imageio_ffmpeg


st.set_page_config(
    page_title="AI YouTube Clipper",
    page_icon="🎬",
    layout="centered"
)

st.title("🎬 AI YouTube Clipper")
st.write("YouTube videos ko AI ki help se short clips mein badlo.")

api_key = st.secrets.get("GEMINI_API_KEY", "")

if not api_key:
    st.error("Gemini API Key missing hai. Streamlit Cloud Secrets mein add karo.")
    st.stop()

client = genai.Client(api_key=api_key)


def get_seconds(value):
    if isinstance(value, (int, float)):
        return int(value)

    parts = str(value).strip().split(":")
    try:
        result = 0
        for part in parts:
            result = result * 60 + float(part)
        return int(result)
    except Exception:
        return 0


def download_video(url, folder, is_live):
    output = os.path.join(folder, "video.%(ext)s")

    command = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "-f", "best[height<=720]/best",
        "-o", output,
        "--merge-output-format", "mp4"
    ]

    if is_live:
        command += ["--hls-use-mpegts"]

    command.append(url)

    if not is_live:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=900
        )

        if result.returncode != 0:
            raise Exception(result.stderr[-5000:])

    else:
        log_path = os.path.join(folder, "download.log")

        with open(log_path, "w") as log_file:
            process = subprocess.Popen(
                command,
                stdout=log_file,
                stderr=subprocess.STDOUT
            )

            try:
                process.wait(timeout=1800)
            except subprocess.TimeoutExpired:
                process.terminate()

                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

        if process.returncode not in (0, -15, 1):
            with open(log_path, "r") as log_file:
                error_text = log_file.read()
            raise Exception(error_text[-5000:])

    files = [
        os.path.join(folder, name)
        for name in os.listdir(folder)
        if name.startswith("video.")
        and not name.endswith((".part", ".ytdl"))
    ]

    if not files:
        log_path = os.path.join(folder, "download.log")
        error_text = ""

        if os.path.exists(log_path):
            with open(log_path, "r") as f:
                error_text = f.read()

        raise Exception("Video file nahi mili.\n" + error_text[-4000:])

    video = max(files, key=os.path.getsize)

    if os.path.getsize(video) == 0:
        raise Exception("Downloaded video empty hai.")

    return video


def analyze_video(video_path):
    st.write("📤 Gemini par video upload ho rahi hai...")

    uploaded_file = client.files.upload(file=video_path)

    for _ in range(60):
        uploaded_file = client.files.get(name=uploaded_file.name)

        if uploaded_file.state and uploaded_file.state.name == "ACTIVE":
            break

        if uploaded_file.state and uploaded_file.state.name == "FAILED":
            raise Exception("Gemini video processing failed.")

        time.sleep(5)

    prompt = """
    Analyze this video and find up to 5 interesting moments
    suitable for YouTube Shorts.

    Return ONLY valid JSON in this exact format:

    {
      "clips": [
        {
          "title": "Clip title",
          "start_sec": 10,
          "end_sec": 45
        }
      ]
    }

    Rules:
    - Maximum 5 clips.
    - Each clip should be 30 to 60 seconds.
    - Use timestamps in seconds.
    - Do not invent moments.
    - Keep clips within the actual video duration.
    """

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[uploaded_file, prompt]
    )

    text = response.text or ""
    match = re.search(r"\{.*\}", text, re.DOTALL)

    if not match:
        raise Exception("Gemini se valid clip timestamps nahi mile:\n" + text)

    data = json.loads(match.group(0))
    return data.get("clips", [])


def create_clip(video, start, end, output):
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

    duration = end - start

    command = [
        ffmpeg,
        "-y",
        "-ss", str(start),
        "-i", video,
        "-t", str(duration),
        "-vf",
        "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-c:a", "aac",
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


url = st.text_input(
    "YouTube Video / Live Stream Link",
    placeholder="https://www.youtube.com/watch?v=..."
)

is_live = st.checkbox(
    "Ye LIVE stream hai (current point se 30 minute record karo)"
)

if is_live:
    st.info("Live stream ko current point se 30 minute tak record karne ki koshish hogi.")

if st.button("🚀 Generate AI Clips", type="primary"):
    if not url.startswith((
        "https://www.youtube.com/",
        "https://youtu.be/",
        "https://youtube.com/"
    )):
        st.error("Valid YouTube URL enter karo.")

    else:
        try:
            with tempfile.TemporaryDirectory() as temp:
                with st.spinner(
                    "LIVE recording chal rahi hai..." if is_live
                    else "YouTube video download ho rahi hai..."
                ):
                    video = download_video(url, temp, is_live)

                st.success("Video download / recording complete!")

                with st.spinner("Gemini AI video analyze kar raha hai..."):
                    clips = analyze_video(video)

                if not clips:
                    st.warning("Is video mein suitable clips nahi mile.")
                    st.stop()

                st.success(f"{len(clips)} clips mile!")

                for index, clip in enumerate(clips, start=1):
                    start = get_seconds(clip.get("start_sec", 0))
                    end = get_seconds(clip.get("end_sec", 0))

                    if end <= start or end - start > 90:
                        continue

                    output = os.path.join(
                        temp,
                        f"clip_{index}.mp4"
                    )

                    with st.spinner(f"Clip {index} ban rahi hai..."):
                        create_clip(video, start, end, output)

                    st.subheader(
                        f"Clip {index}: {clip.get('title', 'AI Clip')}"
                    )

                    st.video(output)

                    with open(output, "rb") as file:
                        st.download_button(
                            label=f"Download Clip {index}",
                            data=file.read(),
                            file_name=f"ai_clip_{index}.mp4",
                            mime="video/mp4",
                            key=f"download_{index}"
                        )

        except subprocess.TimeoutExpired:
            st.error("Download mein bahut time lag gaya. Shorter video try karo.")

        except Exception as e:
            st.error("Something went wrong:")
            st.code(str(e)[-5000:])
