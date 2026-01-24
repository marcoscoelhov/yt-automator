from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips
import os

TEMP_DIR = r"c:\Users\Marcos\Documents\APPS\yt-automator\backend\temp"
STATIC_DIR = r"c:\Users\Marcos\Documents\APPS\yt-automator\backend\static"
audio_path = os.path.join(TEMP_DIR, "audio_1769108244.mp3")
image_path = os.path.join(TEMP_DIR, "scene_1769108244_1.jpg")
output_path = os.path.join(STATIC_DIR, "debug_test.mp4")

print(f"Testing render...")
print(f"Audio: {os.path.exists(audio_path)}")
print(f"Image: {os.path.exists(image_path)}")

try:
    audio_clip = AudioFileClip(audio_path)
    print(f"Audio duration: {audio_clip.duration}")
    
    clip = ImageClip(image_path).set_duration(audio_clip.duration).resize(height=720).set_position("center")
    
    # Simple concatenate (even with 1 clip to test the function)
    final = concatenate_videoclips([clip], method="compose")
    final = final.set_audio(audio_clip)
    
    final.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac")
    print("SUCCESS")
except Exception as e:
    print(f"FAILED: {e}")
    import traceback
    traceback.print_exc()
