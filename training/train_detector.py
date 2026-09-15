"""Fine-tune a small YOLO to find robots by appearance rather than movement.

This is the piece that closes the root gap. Motion can only see a robot that is
moving, so a stationary one is invisible and a lost track can never be recovered.
A detector answers "where are the robots" every frame regardless of movement,
which is what lets tracking re-anchor instead of propagating blindly for fifteen
minutes.

The honest caveat, which belongs next to the code and not only in a report: every
label available today comes from one camera on one field. The model will very
likely learn this view. Held-out numbers here are across *time*, not across
cameras, and say nothing about a new venue until a second calibrated angle exists.
"""
import os

DEFAULT_MODEL = "yolo11n.pt"          # smallest; the dataset is small too
DEFAULT_EPOCHS = 60
DEFAULT_IMAGE_SIZE = 960              # robots at the far wall are small in frame
WEIGHTS_DIR = os.path.join("training", "weights")


def train(dataset_yaml, model=DEFAULT_MODEL, epochs=DEFAULT_EPOCHS, image_size=DEFAULT_IMAGE_SIZE,
          project=WEIGHTS_DIR, name="robots", log=print):
    from ultralytics import YOLO

    log(f"fine-tuning {model} for {epochs} epochs at {image_size}px")
    detector = YOLO(model)
    results = detector.train(
        data=os.path.abspath(dataset_yaml),
        epochs=epochs,
        imgsz=image_size,
        project=os.path.abspath(project),
        name=name,
        exist_ok=True,
        pretrained=True,
        # The camera never moves and the field is always the same way up, so
        # flips and rotations would only invent views that cannot occur. Mild
        # photometric jitter is kept, since lighting does vary between venues.
        fliplr=0.0,
        flipud=0.0,
        degrees=0.0,
        scale=0.3,
        hsv_h=0.015,
        hsv_s=0.5,
        hsv_v=0.4,
        mosaic=0.0,          # mosaic would paste several fields into one frame
        verbose=False,
        plots=False,
    )
    best = os.path.join(project, name, "weights", "best.pt")
    log(f"best weights: {best}")
    return best, results


def evaluate(weights, dataset_yaml, image_size=DEFAULT_IMAGE_SIZE, log=print):
    """Precision and recall on the held-out split only."""
    from ultralytics import YOLO

    metrics = YOLO(weights).val(data=os.path.abspath(dataset_yaml), imgsz=image_size,
                                verbose=False, plots=False)
    summary = {
        "precision": float(metrics.box.mp),
        "recall": float(metrics.box.mr),
        "map50": float(metrics.box.map50),
        "map50_95": float(metrics.box.map),
    }
    log(f"held-out: precision {summary['precision']:.3f}  recall {summary['recall']:.3f}  "
        f"mAP50 {summary['map50']:.3f}")
    log("this measures generalisation across time on one camera - not across cameras")
    return summary


if __name__ == "__main__":
    import argparse

    from training.export_labels import export

    parser = argparse.ArgumentParser(description="Train the robot detector.")
    parser.add_argument("--epochs", type=int, default=DEFAULT_EPOCHS)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--image-size", type=int, default=DEFAULT_IMAGE_SIZE)
    parser.add_argument("--skip-export", action="store_true")
    args = parser.parse_args()

    yaml_path = os.path.join("training", "dataset", "robots.yaml")
    if not args.skip_export:
        yaml_path, _, _ = export()

    weights, _ = train(yaml_path, model=args.model, epochs=args.epochs, image_size=args.image_size)
    evaluate(weights, yaml_path, image_size=args.image_size)
