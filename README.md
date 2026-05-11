# pyav1plus

**pyav1plus** is a Python-based tool for extracting **HDR10+ dynamic metadata** from **AV1** video streams.  
The project supports multiple AV1 container formats, including **IVF**, **MP4/fMP4**, and **Matroska (MKV)**, with direct parsing of AV1 OBUs and HDR10+ ITU-T T.35 metadata.

---

## Features

* Support for **AV1 IVF**
* Support for **AV1 MP4**
* Support for **AV1 fragmented MP4 (fMP4)**
* Support for **AV1 Matroska (MKV)**
* Direct parsing of **AV1 OBU metadata**
* Extraction of **HDR10+ ITU-T T.35 metadata**
* Automatic generation of classic HDR10+ JSON
* Stream-oriented parsing for large files
* Automatic input format detection

---

## Requirements

* Python 3.9 or higher

No external binaries or dependencies are required.

---

## Usage

Basic:

```bash
python pyav1plus.py input.ivf
```

```bash
python pyav1plus.py input.mp4
```

```bash
python pyav1plus.py input.mkv
```

Custom output path:

```bash
python pyav1plus.py input.mp4 -o metadata.json
```

---

## Supported Formats

| Format | Supported |
| --- | --- |
| AV1 IVF | Yes |
| AV1 MP4 | Yes |
| AV1 Fragmented MP4 | Yes |
| AV1 MKV | Yes |

---

## Output

The tool generates a classic HDR10+ JSON structure containing:

* `JSONInfo`
* `SceneInfo`
* `SceneInfoSummary`
* `ToolInfo`

---

## Example

Input:

```bash
python pyav1plus.py movie.mkv
```

Output:

```text
Input format: MKV
Scanning AV1 samples for HDR10+ metadata...
[■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■] 100.00%

Reordering metadata... Done.
Reading parsed dynamic metadata... Done.
Generating and writing metadata to JSON file...
Generating and writing metadata to JSON file... Done.
Frames written: 209557
Output: movie.json
```

---

## Script Example

```python
import json
import sys
import pyav1plus

input_file = "input.mkv"
output_file = "metadata.json"

try:
    input_format = pyav1plus.detect_input_format(input_file)

    if input_format == "ivf":
        iterator = pyav1plus.iter_ivf_samples(input_file)
    elif input_format == "mp4":
        iterator = pyav1plus.iter_mp4_samples(input_file)
    elif input_format == "mkv":
        iterator = pyav1plus.iter_mkv_samples(input_file)
    else:
        raise ValueError("Unsupported input format")

    t35_by_frame = []

    for index, payload, total in iterator:
        t35 = pyav1plus.extract_t35_from_av1_sample(payload, index)
        t35_by_frame.append(t35)

    metadata = pyav1plus.build_classic_json_from_t35_list(t35_by_frame)

    with open(output_file, "w", encoding="utf-8") as file:
        json.dump(metadata, file, indent=2)

except KeyboardInterrupt:
    raise SystemExit(130)
except Exception as error:
    print(f"Extraction failed: {error}", file=sys.stderr)
    raise SystemExit(1)
```

---

## Notes

This project focuses specifically on **AV1 HDR10+ metadata extraction**.  
The parser operates directly on AV1 bitstreams and container structures without relying on external multimedia frameworks.

Different MP4 layouts are supported, including fragmented and non-fragmented variants commonly found in streaming workflows.

---

## Issues and Support

If you encounter any issues, please open an issue in the repository.

---

## Acknowledgements

Thank you for your interest in this project.
