

import streamlit as st

import os

import json

import tempfile

import subprocess

import time

import imageio_ffmpeg

from google import genai

from google.genai import types

st.set_page_config(page_title="AI YouTube Clipper", page_icon="🎬")

st.title("🎬 AI YouTube Clipper")

st.caption("Powered by Gemini AI")

st.markdown("Paste a YouTube video link to create Shorts.")

api_key = st.secrets.get("GEMINI_API_KEY", "")

url = st.text_input("YouTube Video URL")

duration = st.selectbox("Clip duration", [30, 45, 60])

if st.button("🚀 Generate AI Clips"):

    if not api_key:

        st.error("Gemini API Key is missing. Add it in Cloud Secrets.")

        st.stop()

    if not url.startswith(("https://www.youtube.com/", "https://youtu.be/", "https://youtube.com/")):

        st.error("Please enter a valid YouTube URL.")

        st.stop()

    try:

        with st.spinner("Downloading video..."):

            with tempfile.TemporaryDirectory() as temp:

                output = os.path.join(temp, "video.%(ext)s")

                subprocess.run([

                    "yt-dlp",

                    "--no-playlist",

                    "-f", "best[height<=720]/best",

                    "-o", output,

                    url

                ], check=True, timeout=600)

                videos = [

                    os.path.join(temp, f)

                    for f in os.listdir(temp)

                    if f.startswith("video.")

                ]

                if not videos:

                    st.error("Video download failed.")

                    st.stop()

                video = videos[0]

                st.write("Uploading video to Gemini...")

                client = genai.Client(api_key=api_key)

                uploaded = client.files.upload(file=video)

                while uploaded.state.name == "PROCESSING":

                    time.sleep(5)

                    uploaded = client.files.get(name=uploaded.name)

                if uploaded.state.name == "FAILED":

                    st.error("Gemini could not process this video.")

                    st.stop()

                prompt = f"""

                Analyze this video and find its best interesting moments.

                Return ONLY valid JSON in this format:

                {{

                  "clips": [

                    {{

                      "start": 10,

                      "end": 40,

                      "title": "Interesting moment"

                    }}

                  ]

                }}

                Find up to 5 different clips.

                Each clip should be around {duration} seconds.

                Start and end must be valid video timestamps in seconds.

                """

                response = client.models.generate_content(

                    model="gemini-2.5-flash",

                    contents=[

                        types.Part.from_uri(

                            file_uri=uploaded.uri,

                            mime_type=uploaded.mime_type

                        ),

                        prompt

                    ],

                    config=types.GenerateContentConfig(

                        response_mime_type="application/json"

                    )

                )

                data = json.loads(response.text)

                ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

                st.success("AI found your best moments!")

                for i, clip in enumerate(data["clips"]):

                    start = max(0, float(clip["start"]))

                    end = float(clip["end"])

                    if end <= start:

                        continue

                    output_clip = os.path.join(temp, f"clip_{i+1}.mp4")

                    subprocess.run([

                        ffmpeg, "-y",

                        "-ss", str(start),

                        "-i", video,

                        "-t", str(end - start),

                        "-vf",

                        "scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280",

                        "-c:v", "libx264",

                        "-preset", "ultrafast",

                        "-c:a", "aac",

                        output_clip

                    ], check=True, capture_output=True)

                    st.subheader(clip["title"])

                    with open(output_clip, "rb") as f:

                        st.download_button(

                            "⬇️ Download Short",

                            f,

                            file_name=f"short_{i+1}.mp4",

                            mime="video/mp4",

                            key=f"download_{i}"

                        )

                client.files.delete(name=uploaded.name)

    except Exception as e:

        st.error(f"Something went wrong: {e}")

        st.info("Try a shorter public video or check your API quota.")
