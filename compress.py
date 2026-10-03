import io
import re
import zipfile
from pathlib import Path

import streamlit as st
from PIL import Image, ImageFile, ImageOps

ImageFile.LOAD_TRUNCATED_IMAGES = True

st.set_page_config(
    page_title="Bulk Image Compressor",
    page_icon="🗜️",
    layout="wide",
)

def has_transparency(image: Image.Image) -> bool:
    return (
        image.mode in ("RGBA", "LA")
        or (image.mode == "P" and "transparency" in image.info)
    )


def choose_output_format(choice: str, source_format: str, image: Image.Image) -> str:
    if choice == "JPEG (recommended)":
        return "JPEG"
    if choice == "WEBP":
        return "WEBP"

    source_format = (source_format or "").upper()
    if source_format in ("JPG", "JPEG"):
        return "JPEG"
    if source_format == "PNG":
        return "PNG"
    if source_format == "WEBP":
        return "WEBP"

    return "WEBP" if has_transparency(image) else "JPEG"


def prepare_for_format(image: Image.Image, output_format: str) -> Image.Image:
    if output_format == "JPEG":
        if has_transparency(image):
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, "white")
            background.paste(rgba, mask=rgba.getchannel("A"))
            return background
        return image.convert("RGB")

    if output_format == "WEBP":
        return image.convert("RGBA" if has_transparency(image) else "RGB")

    if output_format == "PNG":
        return image.convert("RGBA" if has_transparency(image) else "RGB")

    return image.convert("RGB")


def extract_metadata(image: Image.Image, keep_metadata: bool) -> dict:
    if not keep_metadata:
        return {}

    metadata = {}

    icc_profile = image.info.get("icc_profile")
    if icc_profile:
        metadata["icc_profile"] = icc_profile

    try:
        exif = image.getexif()
        if exif:
            # The pixels are already orientation-corrected by ImageOps.exif_transpose.
            exif[274] = 1
            metadata["exif"] = exif.tobytes()
    except Exception:
        pass

    return metadata


def encode_image(
    image: Image.Image,
    output_format: str,
    quality: int,
    metadata: dict,
) -> bytes:
    buffer = io.BytesIO()

    if output_format == "JPEG":
        save_args = {
            "format": "JPEG",
            "quality": int(quality),
            "optimize": True,
            "progressive": True,
            "subsampling": 1,
        }
        if metadata.get("exif"):
            save_args["exif"] = metadata["exif"]
        if metadata.get("icc_profile"):
            save_args["icc_profile"] = metadata["icc_profile"]

    elif output_format == "WEBP":
        save_args = {
            "format": "WEBP",
            "quality": int(quality),
            "method": 6,
            "lossless": False,
        }
        if metadata.get("exif"):
            save_args["exif"] = metadata["exif"]
        if metadata.get("icc_profile"):
            save_args["icc_profile"] = metadata["icc_profile"]

    elif output_format == "PNG":
        save_args = {
            "format": "PNG",
            "optimize": True,
            "compress_level": 9,
        }
        if metadata.get("icc_profile"):
            save_args["icc_profile"] = metadata["icc_profile"]

    else:
        raise ValueError(f"Unsupported output format: {output_format}")

    image.save(buffer, **save_args)
    return buffer.getvalue()


def find_best_quality(
    image: Image.Image,
    output_format: str,
    max_bytes: int,
    min_quality: int,
    max_quality: int,
    metadata: dict,
):
    # PNG does not have a useful visual-quality slider in Pillow.
    if output_format == "PNG":
        data = encode_image(image, output_format, max_quality, metadata)
        return data, None, len(data) <= max_bytes

    highest_quality_data = encode_image(
        image, output_format, max_quality, metadata
    )
    if len(highest_quality_data) <= max_bytes:
        return highest_quality_data, max_quality, True

    low = min_quality
    high = max_quality
    best_data = None
    best_quality = None

    while low <= high:
        middle = (low + high) // 2
        data = encode_image(image, output_format, middle, metadata)

        if len(data) <= max_bytes:
            best_data = data
            best_quality = middle
            low = middle + 1
        else:
            high = middle - 1

    if best_data is not None:
        return best_data, best_quality, True

    minimum_quality_data = encode_image(
        image, output_format, min_quality, metadata
    )
    return minimum_quality_data, min_quality, False


def compress_to_target(
    source_bytes: bytes,
    output_choice: str,
    min_kb: int,
    max_kb: int,
    min_quality: int,
    max_quality: int,
    keep_metadata: bool,
):
    """Compress purely by adjusting quality/format. Pixel dimensions are
    never changed - the output image always keeps the exact width/height
    (and therefore aspect ratio, e.g. 9:16) of the input image."""
    min_bytes = min_kb * 1024
    max_bytes = max_kb * 1024

    with Image.open(io.BytesIO(source_bytes)) as opened:
        source_format = opened.format or "UNKNOWN"
        image = ImageOps.exif_transpose(opened)
        image.load()

        metadata = extract_metadata(image, keep_metadata)
        output_format = choose_output_format(
            output_choice, source_format, image
        )
        working = prepare_for_format(image, output_format)

    original_dimensions = working.size

    final_data, final_quality, fit_target = find_best_quality(
        working,
        output_format,
        max_bytes,
        min_quality,
        max_quality,
        metadata,
    )

    final_size = len(final_data)
    if final_size < min_bytes:
        target_note = "Below range; no artificial padding added"
    elif final_size <= max_bytes:
        target_note = "Within target range"
    else:
        target_note = "Above target; try lowering minimum quality (dimensions are never reduced)"

    extension = {
        "JPEG": ".jpg",
        "WEBP": ".webp",
        "PNG": ".png",
    }[output_format]

    return {
        "data": final_data,
        "extension": extension,
        "format": output_format,
        "quality": final_quality,
        "dimensions": working.size,
        "original_dimensions": original_dimensions,
        "resized": False,
        "target_note": target_note,
    }


def sanitize_title(title: str) -> str:
    """Keep the title exactly as typed, only stripping characters that are
    illegal in filenames. No extra words/letters are added."""
    title = title.strip()
    title = re.sub(r'[\\/:*?"<>|]', "", title)
    return title


def build_title_groups(titles: list, images_per_title: int, total_images: int):
    """Assign image indices to each title in order, `images_per_title` at a time.
    Returns a list of (title, count) reflecting how many images that title will get
    (the last title may get fewer if uploads run out)."""
    groups = []
    remaining = total_images
    for title in titles:
        if remaining <= 0:
            break
        count = min(images_per_title, remaining)
        groups.append((title, count))
        remaining -= count
    return groups, remaining


# Leading "action" words that show up in front of the real subject and
# should be dropped, e.g. "Create_pork_chop..." -> "pork_chop...".
_LEADING_NOISE_WORDS = {"create", "creating", "how", "to", "make", "making"}

# Standalone filler words that can appear anywhere in the filename and
# should be dropped, e.g. "..._pin_..." -> "...".
_FILLER_WORDS = {"pin", "pins", "img", "image", "compressed", "copy"}


def derive_title_from_filename(filename: str) -> str:
    """Turn a raw uploaded filename into a clean, grouped title:
    - strips the extension
    - drops leading action words (Create/Creating/...)
    - drops long numeric tokens (dates/ids, 6+ digits)
    - drops trailing short numeric tokens (sequence counters like _4, _19)
    - drops filler words like 'pin'
    - lowercases and joins the remaining words with underscores
    """
    stem = Path(filename).stem.lower()
    tokens = [t for t in re.split(r"[_\-\s]+", stem) if t]

    # Drop a trailing short numeric token (counter), e.g. ..._4, ..._19
    if tokens and tokens[-1].isdigit() and len(tokens[-1]) <= 4:
        tokens = tokens[:-1]

    # Drop long numeric tokens anywhere (date-like ids), e.g. 202609072013
    tokens = [t for t in tokens if not (t.isdigit() and len(t) >= 6)]

    # Drop a trailing short numeric token again in case one was hidden
    # behind a date token, e.g. ..._pin_202609072013_4 already handled above,
    # but cover ..._202609072013_pin_4 style ordering too.
    if tokens and tokens[-1].isdigit() and len(tokens[-1]) <= 4:
        tokens = tokens[:-1]

    # Drop leading action words (only from the front, one or more)
    while tokens and tokens[0] in _LEADING_NOISE_WORDS:
        tokens = tokens[1:]

    # Drop filler words anywhere
    tokens = [t for t in tokens if t not in _FILLER_WORDS]

    title = "_".join(tokens).strip("_")
    return sanitize_title(title)


def group_files_by_detected_title(uploaded_files: list):
    """Group uploaded files by their auto-detected title, preserving upload
    order both across groups (first-seen order) and within each group.

    Some filenames carry no real subject words at all once numbers/dates and
    filler words (e.g. "20260915140338_compressed") are stripped out. Those
    files don't start a new group - they're attached to whichever title group
    came right before them, continuing that group's numbering, since they're
    treated as "more of the same" image set. If such a file appears before
    any real title has been seen, it falls back to a generic "image" group.
    """
    order = []
    groups = {}
    last_title = None
    for uploaded_file in uploaded_files:
        title = derive_title_from_filename(uploaded_file.name)
        if not title:
            title = last_title or "image"
        else:
            last_title = title
        if title not in groups:
            groups[title] = []
            order.append(title)
        groups[title].append(uploaded_file)
    return [(title, groups[title]) for title in order]


st.title("🗜️ Bulk Image Compressor")
st.write(
    "Compress many images together and download them as one ZIP file. "
    "The app keeps the highest possible quality under your selected maximum size. "
    "Pixel dimensions (and aspect ratio) are never changed — only quality/format "
    "compression is applied."
)

with st.sidebar:
    st.header("Compression settings")

    min_kb = st.number_input(
        "Preferred minimum size (KB)",
        min_value=20,
        max_value=5000,
        value=200,
        step=10,
    )
    max_kb = st.number_input(
        "Maximum size (KB)",
        min_value=30,
        max_value=10000,
        value=300,
        step=10,
    )

    output_choice = st.selectbox(
        "Output format",
        ["WebP (recommended)", "JPEG", "Keep original format"],
        index=0,
    )

    min_quality = st.slider(
        "Lowest allowed quality",
        min_value=20,
        max_value=90,
        value=55,
    )
    max_quality = st.slider(
        "Preferred maximum quality",
        min_value=70,
        max_value=100,
        value=95,
    )

    keep_metadata = st.checkbox(
        "Keep EXIF and color profile metadata",
        value=False,
        help="Metadata can add extra file size.",
    )

    st.caption(
        "Dimensions are never resized — every output image keeps the exact "
        "width, height, and aspect ratio (e.g. 9:16) of the original upload. "
        "Only quality/format compression is used to reach the target size."
    )

    st.header("Title-based naming")
    naming_mode = st.radio(
        "How should output files be named?",
        ["Auto-detect title from filename", "Manual title list", "Keep original filenames"],
        index=0,
        help=(
            "Auto-detect groups files that share the same underlying subject "
            "(ignoring words like 'Create'/'Creating', dates, and trailing pin "
            "numbers) and renames them title_1, title_2, title_3... "
            "Manual lets you type your own titles instead."
        ),
    )

    titles_text = ""
    images_per_title = None
    if naming_mode == "Manual title list":
        titles_text = st.text_area(
            "Titles (one per line)",
            placeholder="Sunset Beach\nMountain Trail\nCity Lights",
            height=150,
            help=(
                "Images will be assigned to these titles in the order you upload "
                "them. Output files are renamed as title_1, title_2, title_3, "
                "title_4 (no extra suffixes)."
            ),
        )
        images_per_title = st.selectbox(
            "Images per title",
            [2, 3, 4],
            index=2,
        )

if min_kb >= max_kb:
    st.error("The preferred minimum must be smaller than the maximum.")
    st.stop()

if min_quality > max_quality:
    st.error("Lowest quality cannot be higher than preferred maximum quality.")
    st.stop()

uploaded_files = st.file_uploader(
    "Upload JPG, JPEG, PNG, or WebP images",
    type=["jpg", "jpeg", "png", "webp"],
    accept_multiple_files=True,
)

titles = [sanitize_title(t) for t in titles_text.splitlines() if t.strip()]

if uploaded_files:
    st.info(f"{len(uploaded_files)} image(s) selected.")

    detected_groups = []
    manual_groups = []

    if naming_mode == "Auto-detect title from filename":
        detected_groups = group_files_by_detected_title(uploaded_files)
        with st.expander("Preview detected title assignment", expanded=True):
            for title, files in detected_groups:
                names = ", ".join(f"{title}_{i}" for i in range(1, len(files) + 1))
                st.write(f"**{title}** → {len(files)} image(s): {names}")

    elif naming_mode == "Manual title list":
        if titles:
            manual_groups, leftover_images = build_title_groups(
                titles, images_per_title, len(uploaded_files)
            )
            if leftover_images > 0:
                st.warning(
                    f"{leftover_images} uploaded image(s) have no title left to "
                    f"attach to and will be skipped. Add more titles or upload "
                    f"fewer images."
                )
            if len(manual_groups) < len(titles):
                unused_titles = len(titles) - len(manual_groups)
                st.warning(
                    f"{unused_titles} title(s) have no images assigned "
                    f"(not enough images uploaded)."
                )
            with st.expander("Preview title assignment", expanded=False):
                for title, count in manual_groups:
                    names = ", ".join(f"{title}_{i}" for i in range(1, count + 1))
                    st.write(f"**{title}** → {count} image(s): {names}")
        else:
            st.info("Enter at least one title in the sidebar to use manual naming.")

    if st.button("Compress all images", type="primary"):
        results = []
        zip_buffer = io.BytesIO()
        progress = st.progress(0)
        status = st.empty()

        # Build a flat list of (uploaded_file, output_stem) according to the
        # selected naming mode.
        naming_plan = []
        if naming_mode == "Auto-detect title from filename":
            for title, files in detected_groups:
                for i, uploaded_file in enumerate(files, start=1):
                    naming_plan.append((uploaded_file, f"{title}_{i}"))

        elif naming_mode == "Manual title list" and titles:
            cursor = 0
            for title, count in manual_groups:
                for i in range(1, count + 1):
                    naming_plan.append((uploaded_files[cursor], f"{title}_{i}"))
                    cursor += 1

        else:
            for uploaded_file in uploaded_files:
                naming_plan.append((uploaded_file, Path(uploaded_file.name).stem))

        with zipfile.ZipFile(
            zip_buffer,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            total = len(naming_plan)
            for index, (uploaded_file, output_stem) in enumerate(naming_plan, start=1):
                status.write(f"Processing {index}/{total}: {uploaded_file.name}")

                source_bytes = uploaded_file.getvalue()
                original_size_kb = len(source_bytes) / 1024

                try:
                    compressed = compress_to_target(
                        source_bytes=source_bytes,
                        output_choice=output_choice,
                        min_kb=int(min_kb),
                        max_kb=int(max_kb),
                        min_quality=int(min_quality),
                        max_quality=int(max_quality),
                        keep_metadata=keep_metadata,
                    )

                    output_name = output_stem + compressed["extension"]
                    archive.writestr(output_name, compressed["data"])

                    new_size_kb = len(compressed["data"]) / 1024
                    reduction = (
                        (1 - new_size_kb / original_size_kb) * 100
                        if original_size_kb
                        else 0
                    )

                    quality_text = (
                        compressed["quality"]
                        if compressed["quality"] is not None
                        else "Lossless PNG"
                    )

                    results.append(
                        {
                            "File": uploaded_file.name,
                            "Output": output_name,
                            "Original KB": round(original_size_kb, 1),
                            "Compressed KB": round(new_size_kb, 1),
                            "Reduction": f"{reduction:.1f}%",
                            "Quality": quality_text,
                            "Dimensions": (
                                f"{compressed['dimensions'][0]} × "
                                f"{compressed['dimensions'][1]}"
                            ),
                            "Status": compressed["target_note"],
                        }
                    )

                except Exception as error:
                    results.append(
                        {
                            "File": uploaded_file.name,
                            "Output": "—",
                            "Original KB": round(original_size_kb, 1),
                            "Compressed KB": "—",
                            "Reduction": "—",
                            "Quality": "—",
                            "Dimensions": "—",
                            "Status": f"Error: {error}",
                        }
                    )

                progress.progress(index / total)

        status.success("Compression completed.")
        st.session_state["compressed_zip"] = zip_buffer.getvalue()
        st.session_state["compression_results"] = results

if "compression_results" in st.session_state:
    st.subheader("Results")
    st.dataframe(
        st.session_state["compression_results"],
        use_container_width=True,
        hide_index=True,
    )

if "compressed_zip" in st.session_state:
    st.download_button(
        "Download compressed images as ZIP",
        data=st.session_state["compressed_zip"],
        file_name="compressed_images.zip",
        mime="application/zip",
        type="primary",
    )

st.caption(
    "Note: Reaching 200–300 KB with absolutely zero quality loss is not possible "
    "for every image. This app aims for visually lossless results at your original "
    "dimensions by selecting the highest quality that fits — it will never shrink "
    "the image's width/height to hit the target size."
)
