# image_to_video_fast_script.py
from pathlib import Path
import cv2


def images_to_video(
    folder_path: str,
    output_path: str = "slideshow.mp4",
    seconds_per_image: float = 3.5,
    fps: int = 30,
) -> str:
    """
    Build a slideshow video from all JPG/PNG images in folder_path.
    Returns the output video path.
    """
    img_dir = Path(folder_path)
    imgs = sorted([p for p in img_dir.iterdir()
                  if p.suffix.lower() in {".jpg", ".jpeg", ".png"}])
    if not imgs:
        raise FileNotFoundError("No images found in folder.")

    first = cv2.imread(str(imgs[0]))
    if first is None:
        raise ValueError(f"Failed to read first image: {imgs[0]}")
    h, w, _ = first.shape
    frames_per_image = max(1, int(fps * seconds_per_image))

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter(str(output_path), fourcc, fps, (w, h))

    for img_path in imgs:
        frame = cv2.imread(str(img_path))
        if frame is None:
            continue
        frame = cv2.resize(frame, (w, h))
        for _ in range(frames_per_image):
            out.write(frame)

    out.release()
    return str(output_path)


# Example call from another module:
# images_to_video("/path/to/images", output_path="out.mp4")
default_folder = "/path/to/repo/EdennCode/WorkflowExamples/multi_image_workflow/multi_image_design"


images_to_video(folder_path=default_folder)
