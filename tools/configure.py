from pathlib import Path

import shutil

assets_dir = Path(__file__).parent.parent.resolve() / "assets"

# OCR 模型必须有的三个文件
REQUIRED_FILES = ("det.onnx", "rec.onnx", "keys.txt")


def _has_model(ocr_dir: Path) -> bool:
    return all((ocr_dir / name).exists() for name in REQUIRED_FILES)


def configure_ocr_model():
    ocr_dir = assets_dir / "resource" / "model" / "ocr"

    # 已经有可用的模型就直接用 —— 不要因为子模块没拉就把打包卡死。
    # （子模块 assets/MaaCommonAssets 只是「默认模型」的来源，
    #   本项目已经自带了一份 PP-OCRv6 small，没必要强制要求它。）
    if _has_model(ocr_dir):
        print(f"Found complete OCR model in {ocr_dir}, skipping default OCR model import.")
        return

    assets_ocr_dir = assets_dir / "MaaCommonAssets" / "OCR"
    if not assets_ocr_dir.exists():
        print(f"File Not Found: {assets_ocr_dir}")
        print("")
        print("OCR 模型缺失，且子模块也没拉。二选一：")
        print("  a) 拉子模块：git submodule update --init --recursive")
        print("  b) 手动放模型：把 det.onnx / rec.onnx / keys.txt 放进")
        print(f"     {ocr_dir}")
        exit(1)

    shutil.copytree(
        assets_ocr_dir / "ppocr_v6" / "small",
        ocr_dir,
        dirs_exist_ok=True,
    )
    print(f"Copied default OCR model to {ocr_dir}")


if __name__ == "__main__":
    configure_ocr_model()

    print("OCR model configured.")
