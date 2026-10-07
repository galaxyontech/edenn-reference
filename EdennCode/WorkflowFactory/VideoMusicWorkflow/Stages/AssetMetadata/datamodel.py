from dataclasses import dataclass


@dataclass
class FileHandlerEnum:
    video_post_remix_output: str = 'video_post_remix_output.mp4'
    post_generated_music_output: str = 'post_generated_music_output.wav'
