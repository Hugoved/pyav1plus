from __future__ import annotations
import argparse
import json
import os
import struct
import sys
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Tuple

METADATA_TYPE_ITUT_T35 = 4
HDR10PLUS_COUNTRY_CODE = 0xB5
HDR10PLUS_PROVIDER_CODE = 0x003C
HDR10PLUS_ORIENTED_CODE = 0x0001
HDR10PLUS_APP_ID = 0x04
HDR10PLUS_APP_MODE = 0x01
OBU_METADATA = 5
DIST_INDEX_CLASSIC = [1, 5, 10, 25, 50, 75, 90, 95, 99]
MATROSKA_CONTAINER_IDS = {0x1A45DFA3, 0x18538067, 0x1549A966, 0x1654AE6B, 0xAE, 0x1F43B675, 0xA0}
MATROSKA_TRACKS_ID = 0x1654AE6B
MATROSKA_TRACK_ENTRY_ID = 0xAE
MATROSKA_TRACK_NUMBER_ID = 0xD7
MATROSKA_TRACK_TYPE_ID = 0x83
MATROSKA_CODEC_ID_ID = 0x86
MATROSKA_CLUSTER_ID = 0x1F43B675
MATROSKA_SIMPLE_BLOCK_ID = 0xA3
MATROSKA_BLOCK_GROUP_ID = 0xA0
MATROSKA_BLOCK_ID = 0xA1

def progress_bar(percent: float, width: int = 50) -> None:
    percent = max(0.0, min(100.0, float(percent)))
    filled = int(width * percent / 100.0)
    bar = "■" * filled + " " * (width - filled)
    sys.stderr.write(f"\r[{bar}] {percent:6.2f}%")
    sys.stderr.flush()

def progress_done() -> None:
    progress_bar(100.0)
    sys.stderr.write("\n")
    sys.stderr.flush()

def status(message: str) -> None:
    sys.stderr.write(message + "\n")
    sys.stderr.flush()

def u16be(b: bytes) -> int:
    return (b[0] << 8) | b[1]

def clamp(v: int, lo: int, hi: int) -> int:
    return lo if v < lo else hi if v > hi else v

def leb128_read(data: bytes, off: int) -> Tuple[int, int]:
    val = 0
    shift = 0
    i = 0
    while True:
        if off + i >= len(data):
            raise ValueError("LEB128 truncated")
        byte = data[off + i]
        val |= (byte & 0x7F) << shift
        i += 1
        if (byte & 0x80) == 0:
            break
        shift += 7
        if shift > 63:
            raise ValueError("LEB128 too long")
    return val, i

class BitReader:
    def __init__(self, data: bytes):
        self.data = data
        self.bitpos = 0

    def remaining_bits(self) -> int:
        return len(self.data) * 8 - self.bitpos

    def read_bits(self, n: int) -> int:
        if n <= 0:
            return 0
        if self.remaining_bits() < n:
            raise ValueError("BitReader underflow")
        start = self.bitpos
        end = start + n
        byte_start = start >> 3
        byte_end = (end + 7) >> 3
        value = int.from_bytes(self.data[byte_start:byte_end], "big")
        extra_right_bits = (byte_end << 3) - end
        value >>= extra_right_bits
        value &= (1 << n) - 1
        self.bitpos = end
        return value

    def read_u8(self) -> int:
        return self.read_bits(8)

@dataclass
class IvfHeader:
    width: int
    height: int
    timebase_num: int
    timebase_den: int
    frames: int

@dataclass
class Obu:
    obu_type: int
    header: bytes
    ext: bytes
    payload: bytes

@dataclass
class Box:
    type: bytes
    start: int
    size: int
    header_size: int
    end: int

@dataclass
class FragmentDefaults:
    base_data_offset: Optional[int] = None
    default_sample_duration: Optional[int] = None
    default_sample_size: Optional[int] = None
    default_sample_flags: Optional[int] = None

@dataclass
class SampleRef:
    offset: int
    size: int

@dataclass
class MatroskaElement:
    element_id: int
    data_start: int
    data_size: Optional[int]
    data_end: int
    header_start: int
    header_end: int

@dataclass
class MatroskaTrack:
    number: int
    track_type: int
    codec_id: str

def read_ivf_header(fp) -> IvfHeader:
    header = fp.read(32)
    if len(header) != 32:
        raise ValueError("IVF header truncated")
    if header[0:4] != b"DKIF":
        raise ValueError("Not an IVF file")
    if header[8:12] != b"AV01":
        raise ValueError("IVF stream is not AV1")
    width = struct.unpack_from("<H", header, 12)[0]
    height = struct.unpack_from("<H", header, 14)[0]
    tb_den = struct.unpack_from("<I", header, 16)[0]
    tb_num = struct.unpack_from("<I", header, 20)[0]
    frames = struct.unpack_from("<I", header, 24)[0]
    return IvfHeader(width, height, tb_num, tb_den, frames)

def iter_ivf_samples(path: str) -> Iterator[Tuple[int, bytes, int]]:
    with open(path, "rb") as fp:
        header = read_ivf_header(fp)
        index = 0
        while True:
            frame_header = fp.read(12)
            if not frame_header:
                return
            if len(frame_header) != 12:
                raise ValueError("IVF frame header truncated")
            size = struct.unpack_from("<I", frame_header, 0)[0]
            payload = fp.read(size)
            if len(payload) != size:
                raise ValueError("IVF frame payload truncated")
            yield index, payload, header.frames
            index += 1

def parse_obus(frame_payload: bytes) -> List[Obu]:
    obus: List[Obu] = []
    off = 0
    n = len(frame_payload)
    while off < n:
        first = frame_payload[off]
        off += 1
        forbidden = (first >> 7) & 1
        if forbidden:
            raise ValueError("Invalid OBU header")
        obu_type = (first >> 3) & 0x0F
        ext_flag = (first >> 2) & 1
        has_size_field = (first >> 1) & 1
        ext = b""
        if ext_flag:
            if off >= n:
                raise ValueError("OBU extension truncated")
            ext = frame_payload[off:off + 1]
            off += 1
        if not has_size_field:
            raise ValueError("OBU has_size_field=0 is not supported")
        obu_size, used = leb128_read(frame_payload, off)
        off += used
        if off + obu_size > n:
            raise ValueError("OBU payload truncated")
        payload = frame_payload[off:off + obu_size]
        off += obu_size
        obus.append(Obu(obu_type=obu_type, header=bytes([first]), ext=ext, payload=payload))
    return obus

def is_hdr10plus_t35(t35: bytes) -> bool:
    if len(t35) < 7:
        return False
    if t35[0] != HDR10PLUS_COUNTRY_CODE:
        return False
    provider = u16be(t35[1:3])
    oriented = u16be(t35[3:5])
    app_id = t35[5]
    app_mode = t35[6]
    return provider == HDR10PLUS_PROVIDER_CODE and oriented == HDR10PLUS_ORIENTED_CODE and app_id == HDR10PLUS_APP_ID and app_mode == HDR10PLUS_APP_MODE

def extract_t35_from_metadata_obu_payload(metadata_payload: bytes) -> Optional[bytes]:
    try:
        mtype, used = leb128_read(metadata_payload, 0)
    except Exception:
        return None
    if mtype != METADATA_TYPE_ITUT_T35:
        return None
    t35 = metadata_payload[used:]
    return t35 if is_hdr10plus_t35(t35) else None

def extract_t35_from_av1_sample(sample_payload: bytes, sample_index: int) -> bytes:
    obus = parse_obus(sample_payload)
    for obu in obus:
        if obu.obu_type == OBU_METADATA:
            t35 = extract_t35_from_metadata_obu_payload(obu.payload)
            if t35 is not None:
                return t35
    raise ValueError(f"No HDR10+ T.35 metadata found in frame/sample {sample_index}")

def decode_hdr10plus_t35_av1(t35: bytes) -> Dict:
    if not is_hdr10plus_t35(t35):
        raise ValueError("Not HDR10+ T.35")
    br = BitReader(t35[7:])
    num_windows = br.read_bits(2)
    targeted_raw = br.read_bits(27)
    tsd_actual_flag = br.read_bits(1)
    if tsd_actual_flag:
        rows = br.read_bits(5)
        cols = br.read_bits(5)
        for _r in range(rows):
            for _c in range(cols):
                _ = br.read_bits(4)
    max_scl_raw = [0, 0, 0]
    average_raw = 0
    dist_map: Dict[int, int] = {}
    nw = num_windows if num_windows else 1
    for _w in range(nw):
        max_scl_raw = [br.read_bits(17) for _ in range(3)]
        average_raw = br.read_bits(17)
        num_dists = br.read_bits(4)
        dist_map = {}
        for _ in range(num_dists):
            pct = br.read_bits(7)
            val = br.read_bits(17)
            dist_map[int(pct)] = int(val)
        _ = br.read_bits(10)
    mastering_flag = br.read_bits(1)
    if mastering_flag:
        rows = br.read_bits(5)
        cols = br.read_bits(5)
        for _r in range(rows):
            for _c in range(cols):
                _ = br.read_bits(4)
    knee_x = 0
    knee_y = 0
    anchors: List[int] = [0] * 9
    for _w in range(nw):
        tone_mapping_flag = br.read_bits(1)
        if tone_mapping_flag:
            knee_x = br.read_bits(12)
            knee_y = br.read_bits(12)
            n_anchors = br.read_bits(4)
            raw_anchors = []
            for _ in range(n_anchors):
                raw_anchors.append(br.read_bits(10))
            anchors = (raw_anchors[:9] + [0] * 9)[:9]
    color_flag = br.read_bits(1) if br.remaining_bits() >= 1 else 0
    if color_flag and br.remaining_bits() >= 6:
        _ = br.read_bits(6)
    dist_vals = [int(dist_map.get(i, 0)) for i in DIST_INDEX_CLASSIC]
    targeted_out = int(targeted_raw // 64) if targeted_raw > 10000 else int(targeted_raw)
    max_scl_out = [int(x // 32) for x in max_scl_raw] if any(x > 100000 for x in max_scl_raw) else [int(x) for x in max_scl_raw]
    average_out = int(average_raw // 32) if average_raw > 100000 else int(average_raw)
    return {
        "NumberOfWindows": int(nw),
        "TargetedSystemDisplayMaximumLuminance": targeted_out,
        "AverageRGB": average_out,
        "MaxScl": max_scl_out,
        "DistributionIndex": list(DIST_INDEX_CLASSIC),
        "DistributionValues": dist_vals,
        "BezierCurveData": {
            "Anchors": [int(x) for x in anchors],
            "KneePointX": int(knee_x),
            "KneePointY": int(knee_y),
        },
    }

def metadata_signature(decoded: Dict) -> Tuple:
    return (
        decoded.get("NumberOfWindows"),
        decoded.get("TargetedSystemDisplayMaximumLuminance"),
        decoded.get("AverageRGB"),
        tuple(decoded.get("MaxScl", [])),
        tuple(decoded.get("DistributionValues", [])),
        decoded.get("BezierCurveData", {}).get("KneePointX"),
        decoded.get("BezierCurveData", {}).get("KneePointY"),
        tuple(decoded.get("BezierCurveData", {}).get("Anchors", [])),
    )

def build_classic_json_from_t35_list(t35_by_frame: List[bytes]) -> Dict:
    scene_info: List[Dict] = []
    scene_id = 0
    scene_frame_index = 0
    prev_sig: Optional[Tuple] = None
    for i, t35 in enumerate(t35_by_frame):
        decoded = decode_hdr10plus_t35_av1(t35)
        sig = metadata_signature(decoded)
        if prev_sig is None:
            scene_id = 0
            scene_frame_index = 0
        else:
            if sig != prev_sig:
                scene_id += 1
                scene_frame_index = 0
            else:
                scene_frame_index += 1
        entry = {
            "BezierCurveData": decoded["BezierCurveData"],
            "LuminanceParameters": {
                "AverageRGB": decoded["AverageRGB"],
                "LuminanceDistributions": {
                    "DistributionIndex": decoded["DistributionIndex"],
                    "DistributionValues": decoded["DistributionValues"],
                },
                "MaxScl": decoded["MaxScl"],
            },
            "NumberOfWindows": decoded["NumberOfWindows"],
            "TargetedSystemDisplayMaximumLuminance": decoded["TargetedSystemDisplayMaximumLuminance"],
            "SceneFrameIndex": int(scene_frame_index),
            "SceneId": int(scene_id),
            "SequenceFrameIndex": int(i),
        }
        scene_info.append(entry)
        prev_sig = sig
    scene_first: List[int] = []
    scene_len: List[int] = []
    if scene_info:
        cur = scene_info[0]["SceneId"]
        start = 0
        for idx in range(1, len(scene_info)):
            if scene_info[idx]["SceneId"] != cur:
                scene_first.append(start)
                scene_len.append(idx - start)
                start = idx
                cur = scene_info[idx]["SceneId"]
        scene_first.append(start)
        scene_len.append(len(scene_info) - start)
    return {
        "JSONInfo": {"HDR10plusProfile": "B", "Version": "1.0"},
        "SceneInfo": scene_info,
        "SceneInfoSummary": {
            "SceneFirstFrameIndex": scene_first,
            "SceneFrameNumbers": scene_len,
        },
        "ToolInfo": {"Tool": "pyav1plus", "Version": "1.1.0"},
    }

def read_box_header(fp, file_size: int) -> Optional[Box]:
    start = fp.tell()
    if start >= file_size:
        return None
    header = fp.read(8)
    if not header:
        return None
    if len(header) != 8:
        raise ValueError(f"MP4 box header truncated at offset {start}")
    size32, box_type = struct.unpack(">I4s", header)
    header_size = 8
    if size32 == 1:
        ext = fp.read(8)
        if len(ext) != 8:
            raise ValueError(f"MP4 extended box size truncated at offset {start}")
        size = struct.unpack(">Q", ext)[0]
        header_size = 16
    elif size32 == 0:
        size = file_size - start
    else:
        size = size32
    if size < header_size:
        raise ValueError(f"Invalid MP4 box size at offset {start}")
    return Box(box_type, start, size, header_size, start + size)

def iter_top_level_boxes(fp, file_size: int) -> Iterator[Box]:
    fp.seek(0)
    while fp.tell() < file_size:
        box = read_box_header(fp, file_size)
        if box is None:
            break
        yield box
        fp.seek(box.end)

def iter_child_boxes(fp, start: int, end: int, file_size: int) -> Iterator[Box]:
    fp.seek(start)
    while fp.tell() < end:
        box = read_box_header(fp, file_size)
        if box is None:
            break
        if box.end > end:
            raise ValueError(f"Child MP4 box {box.type!r} exceeds parent bounds")
        yield box
        fp.seek(box.end)

def parse_tfhd(data: bytes, moof_start: int) -> FragmentDefaults:
    if len(data) < 8:
        raise ValueError("tfhd box is truncated")
    version_flags = struct.unpack_from(">I", data, 0)[0]
    flags = version_flags & 0xFFFFFF
    offset = 8
    defaults = FragmentDefaults()
    if flags & 0x000001:
        defaults.base_data_offset = struct.unpack_from(">Q", data, offset)[0]
        offset += 8
    if flags & 0x000002:
        offset += 4
    if flags & 0x000008:
        defaults.default_sample_duration = struct.unpack_from(">I", data, offset)[0]
        offset += 4
    if flags & 0x000010:
        defaults.default_sample_size = struct.unpack_from(">I", data, offset)[0]
        offset += 4
    if flags & 0x000020:
        defaults.default_sample_flags = struct.unpack_from(">I", data, offset)[0]
        offset += 4
    if flags & 0x020000:
        defaults.base_data_offset = moof_start
    return defaults

def parse_trun(data: bytes, defaults: FragmentDefaults, moof_start: int) -> List[SampleRef]:
    if len(data) < 8:
        raise ValueError("trun box is truncated")
    version_flags = struct.unpack_from(">I", data, 0)[0]
    flags = version_flags & 0xFFFFFF
    sample_count = struct.unpack_from(">I", data, 4)[0]
    offset = 8
    data_offset = 0
    if flags & 0x000001:
        data_offset = struct.unpack_from(">i", data, offset)[0]
        offset += 4
    if flags & 0x000004:
        offset += 4
    current = (defaults.base_data_offset if defaults.base_data_offset is not None else moof_start) + data_offset
    samples: List[SampleRef] = []
    for _ in range(sample_count):
        if flags & 0x000100:
            offset += 4
        if flags & 0x000200:
            sample_size = struct.unpack_from(">I", data, offset)[0]
            offset += 4
        else:
            sample_size = defaults.default_sample_size
            if sample_size is None:
                raise ValueError("trun does not contain sample sizes and tfhd has no default_sample_size")
        if flags & 0x000400:
            offset += 4
        if flags & 0x000800:
            offset += 4
        samples.append(SampleRef(int(current), int(sample_size)))
        current += int(sample_size)
    return samples

def collect_fragmented_mp4_samples(path: str) -> List[SampleRef]:
    samples: List[SampleRef] = []
    file_size = os.path.getsize(path)
    with open(path, "rb") as fp:
        for top in iter_top_level_boxes(fp, file_size):
            if top.type != b"moof":
                continue
            for moof_child in iter_child_boxes(fp, top.start + top.header_size, top.end, file_size):
                if moof_child.type != b"traf":
                    continue
                defaults = FragmentDefaults()
                trun_payloads: List[bytes] = []
                for traf_child in iter_child_boxes(fp, moof_child.start + moof_child.header_size, moof_child.end, file_size):
                    fp.seek(traf_child.start + traf_child.header_size)
                    payload = fp.read(traf_child.size - traf_child.header_size)
                    if traf_child.type == b"tfhd":
                        defaults = parse_tfhd(payload, top.start)
                    elif traf_child.type == b"trun":
                        trun_payloads.append(payload)
                for payload in trun_payloads:
                    samples.extend(parse_trun(payload, defaults, top.start))
    return samples

def parse_stsz(data: bytes) -> List[int]:
    sample_size = struct.unpack_from(">I", data, 4)[0]
    sample_count = struct.unpack_from(">I", data, 8)[0]
    if sample_size:
        return [int(sample_size)] * sample_count
    return [struct.unpack_from(">I", data, 12 + i * 4)[0] for i in range(sample_count)]

def parse_stco(data: bytes) -> List[int]:
    count = struct.unpack_from(">I", data, 4)[0]
    return [struct.unpack_from(">I", data, 8 + i * 4)[0] for i in range(count)]

def parse_co64(data: bytes) -> List[int]:
    count = struct.unpack_from(">I", data, 4)[0]
    return [struct.unpack_from(">Q", data, 8 + i * 8)[0] for i in range(count)]

def parse_stsc(data: bytes) -> List[Tuple[int, int, int]]:
    count = struct.unpack_from(">I", data, 4)[0]
    entries = []
    for i in range(count):
        entries.append(struct.unpack_from(">III", data, 8 + i * 12))
    return entries

def collect_unfragmented_mp4_samples(path: str) -> List[SampleRef]:
    file_size = os.path.getsize(path)
    container_boxes = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"mvex", b"meta"}

    def walk(fp, start: int, end: int):
        fp.seek(start)
        while fp.tell() < end:
            box = read_box_header(fp, file_size)
            if box is None:
                return None
            if box.type == b"stbl":
                stsz = stco = co64 = stsc = None
                for child in iter_child_boxes(fp, box.start + box.header_size, box.end, file_size):
                    fp.seek(child.start + child.header_size)
                    payload = fp.read(child.size - child.header_size)
                    if child.type == b"stsz":
                        stsz = parse_stsz(payload)
                    elif child.type == b"stco":
                        stco = parse_stco(payload)
                    elif child.type == b"co64":
                        co64 = parse_co64(payload)
                    elif child.type == b"stsc":
                        stsc = parse_stsc(payload)
                chunk_offsets = co64 if co64 is not None else stco
                if stsz and chunk_offsets and stsc:
                    return stsz, chunk_offsets, stsc
            elif box.type in container_boxes:
                result = walk(fp, box.start + box.header_size, box.end)
                if result is not None:
                    return result
            fp.seek(box.end)
        return None

    with open(path, "rb") as fp:
        result = walk(fp, 0, file_size)
    if result is None:
        return []
    sizes, chunk_offsets, stsc_entries = result
    samples: List[SampleRef] = []
    sample_index = 0
    for chunk_index, chunk_offset in enumerate(chunk_offsets, start=1):
        active = stsc_entries[0]
        for entry_index, entry in enumerate(stsc_entries):
            next_first = stsc_entries[entry_index + 1][0] if entry_index + 1 < len(stsc_entries) else 1 << 60
            if entry[0] <= chunk_index < next_first:
                active = entry
                break
        current = int(chunk_offset)
        for _ in range(active[1]):
            if sample_index >= len(sizes):
                break
            size = int(sizes[sample_index])
            samples.append(SampleRef(current, size))
            current += size
            sample_index += 1
    return samples

def collect_mp4_samples(path: str) -> List[SampleRef]:
    samples = collect_fragmented_mp4_samples(path)
    if samples:
        return samples
    samples = collect_unfragmented_mp4_samples(path)
    if samples:
        return samples
    raise ValueError("No MP4 AV1 samples were found")

def iter_mp4_samples(path: str) -> Iterator[Tuple[int, bytes, int]]:
    samples = collect_mp4_samples(path)
    total = len(samples)
    with open(path, "rb") as fp:
        for index, sample in enumerate(samples):
            fp.seek(sample.offset)
            payload = fp.read(sample.size)
            if len(payload) != sample.size:
                raise ValueError(f"MP4 sample {index} is truncated")
            yield index, payload, total

def read_ebml_id_from_file(fp) -> Optional[Tuple[int, int]]:
    first_raw = fp.read(1)
    if not first_raw:
        return None
    first = first_raw[0]
    mask = 0x80
    length = 1
    while length <= 4 and not (first & mask):
        mask >>= 1
        length += 1
    if length > 4:
        raise ValueError("Invalid EBML element ID")
    rest = fp.read(length - 1)
    if len(rest) != length - 1:
        raise ValueError("Truncated EBML element ID")
    return int.from_bytes(first_raw + rest, "big"), length

def read_ebml_size_from_file(fp) -> Tuple[Optional[int], int]:
    first_raw = fp.read(1)
    if not first_raw:
        raise ValueError("Truncated EBML element size")
    first = first_raw[0]
    mask = 0x80
    length = 1
    while length <= 8 and not (first & mask):
        mask >>= 1
        length += 1
    if length > 8:
        raise ValueError("Invalid EBML element size")
    value = first & (mask - 1)
    rest = fp.read(length - 1)
    if len(rest) != length - 1:
        raise ValueError("Truncated EBML element size")
    for b in rest:
        value = (value << 8) | b
    unknown = value == ((1 << (7 * length)) - 1)
    return None if unknown else value, length

def read_matroska_element(fp, limit: int) -> Optional[MatroskaElement]:
    header_start = fp.tell()
    if header_start >= limit:
        return None
    id_result = read_ebml_id_from_file(fp)
    if id_result is None:
        return None
    element_id, id_len = id_result
    data_size, size_len = read_ebml_size_from_file(fp)
    data_start = fp.tell()
    data_end = limit if data_size is None else data_start + data_size
    if data_end > limit:
        data_end = limit
    return MatroskaElement(element_id, data_start, data_size, data_end, header_start, data_start)

def read_unsigned_element(fp, element: MatroskaElement) -> int:
    fp.seek(element.data_start)
    data = fp.read(element.data_end - element.data_start)
    if not data:
        return 0
    return int.from_bytes(data, "big")

def read_string_element(fp, element: MatroskaElement) -> str:
    fp.seek(element.data_start)
    data = fp.read(element.data_end - element.data_start)
    return data.decode("utf-8", "replace")

def parse_matroska_track_entry(fp, entry: MatroskaElement) -> Optional[MatroskaTrack]:
    number = 0
    track_type = 0
    codec_id = ""
    fp.seek(entry.data_start)
    while fp.tell() < entry.data_end:
        child = read_matroska_element(fp, entry.data_end)
        if child is None:
            break
        if child.element_id == MATROSKA_TRACK_NUMBER_ID:
            number = read_unsigned_element(fp, child)
        elif child.element_id == MATROSKA_TRACK_TYPE_ID:
            track_type = read_unsigned_element(fp, child)
        elif child.element_id == MATROSKA_CODEC_ID_ID:
            codec_id = read_string_element(fp, child)
        fp.seek(child.data_end)
    if number <= 0:
        return None
    return MatroskaTrack(number=number, track_type=track_type, codec_id=codec_id)

def parse_matroska_tracks(fp, tracks_element: MatroskaElement) -> Dict[int, MatroskaTrack]:
    tracks: Dict[int, MatroskaTrack] = {}
    fp.seek(tracks_element.data_start)
    while fp.tell() < tracks_element.data_end:
        child = read_matroska_element(fp, tracks_element.data_end)
        if child is None:
            break
        if child.element_id == MATROSKA_TRACK_ENTRY_ID:
            track = parse_matroska_track_entry(fp, child)
            if track is not None:
                tracks[track.number] = track
        fp.seek(child.data_end)
    return tracks

def collect_matroska_tracks(path: str) -> Dict[int, MatroskaTrack]:
    file_size = os.path.getsize(path)
    tracks: Dict[int, MatroskaTrack] = {}

    def walk(fp, start: int, end: int) -> None:
        fp.seek(start)
        while fp.tell() < end:
            element = read_matroska_element(fp, end)
            if element is None:
                return
            if element.element_id == MATROSKA_TRACKS_ID:
                tracks.update(parse_matroska_tracks(fp, element))
                return
            if element.element_id in MATROSKA_CONTAINER_IDS and element.element_id != MATROSKA_CLUSTER_ID:
                walk(fp, element.data_start, element.data_end)
                if tracks:
                    return
            fp.seek(element.data_end)

    with open(path, "rb") as fp:
        walk(fp, 0, file_size)
    return tracks

def read_block_track_number(data: bytes, off: int = 0) -> Tuple[int, int]:
    if off >= len(data):
        raise ValueError("Matroska block is truncated")
    first = data[off]
    mask = 0x80
    length = 1
    while length <= 8 and not (first & mask):
        mask >>= 1
        length += 1
    if length > 8 or off + length > len(data):
        raise ValueError("Invalid Matroska block track number")
    value = first & (mask - 1)
    for b in data[off + 1:off + length]:
        value = (value << 8) | b
    return value, length

def split_xiph_lacing(data: bytes, off: int, frame_count: int) -> List[bytes]:
    sizes: List[int] = []
    cursor = off
    for _ in range(frame_count - 1):
        size = 0
        while True:
            if cursor >= len(data):
                raise ValueError("Xiph lacing is truncated")
            b = data[cursor]
            cursor += 1
            size += b
            if b != 255:
                break
        sizes.append(size)
    used = sum(sizes)
    remaining = len(data) - cursor - used
    if remaining < 0:
        raise ValueError("Invalid Xiph lacing sizes")
    sizes.append(remaining)
    frames = []
    for size in sizes:
        frames.append(data[cursor:cursor + size])
        cursor += size
    return frames

def read_ebml_vint_from_bytes(data: bytes, off: int) -> Tuple[int, int]:
    if off >= len(data):
        raise ValueError("EBML lacing size is truncated")
    first = data[off]
    mask = 0x80
    length = 1
    while length <= 8 and not (first & mask):
        mask >>= 1
        length += 1
    if length > 8 or off + length > len(data):
        raise ValueError("Invalid EBML lacing size")
    value = first & (mask - 1)
    for b in data[off + 1:off + length]:
        value = (value << 8) | b
    return value, length

def signed_ebml_value(value: int, length: int) -> int:
    bias = (1 << (7 * length - 1)) - 1
    return value - bias

def split_ebml_lacing(data: bytes, off: int, frame_count: int) -> List[bytes]:
    sizes: List[int] = []
    cursor = off
    first_size, used = read_ebml_vint_from_bytes(data, cursor)
    cursor += used
    sizes.append(first_size)
    previous = first_size
    for _ in range(frame_count - 2):
        raw, used = read_ebml_vint_from_bytes(data, cursor)
        diff = signed_ebml_value(raw, used)
        cursor += used
        current = previous + diff
        if current < 0:
            raise ValueError("Invalid EBML lacing size")
        sizes.append(current)
        previous = current
    used_total = sum(sizes)
    remaining = len(data) - cursor - used_total
    if remaining < 0:
        raise ValueError("Invalid EBML lacing sizes")
    sizes.append(remaining)
    frames = []
    for size in sizes:
        frames.append(data[cursor:cursor + size])
        cursor += size
    return frames

def split_fixed_lacing(data: bytes, off: int, frame_count: int) -> List[bytes]:
    remaining = len(data) - off
    if frame_count <= 0 or remaining % frame_count:
        raise ValueError("Invalid fixed lacing size")
    size = remaining // frame_count
    return [data[off + i * size:off + (i + 1) * size] for i in range(frame_count)]

def parse_matroska_block_frames(data: bytes) -> Tuple[int, List[bytes]]:
    track_number, used = read_block_track_number(data, 0)
    cursor = used + 2
    if cursor >= len(data):
        raise ValueError("Matroska block is truncated")
    flags = data[cursor]
    cursor += 1
    lacing = (flags >> 1) & 0x03
    if lacing == 0:
        return track_number, [data[cursor:]]
    if cursor >= len(data):
        raise ValueError("Matroska laced block is truncated")
    frame_count = data[cursor] + 1
    cursor += 1
    if lacing == 1:
        return track_number, split_xiph_lacing(data, cursor, frame_count)
    if lacing == 2:
        return track_number, split_fixed_lacing(data, cursor, frame_count)
    return track_number, split_ebml_lacing(data, cursor, frame_count)

def iter_matroska_block_payloads(path: str) -> Iterator[Tuple[int, bytes, int]]:
    file_size = os.path.getsize(path)
    tracks = collect_matroska_tracks(path)
    av1_tracks = {number for number, track in tracks.items() if track.track_type == 1 and track.codec_id.upper() == "V_AV1"}
    if not av1_tracks and tracks:
        av1_tracks = {number for number, track in tracks.items() if track.codec_id.upper() == "V_AV1"}
    index = 0
    with open(path, "rb") as fp:
        stack: List[Tuple[int, bool]] = [(file_size, False)]
        while stack:
            limit, in_cluster = stack.pop()
            while fp.tell() < limit:
                element = read_matroska_element(fp, limit)
                if element is None:
                    break
                if element.element_id == MATROSKA_CLUSTER_ID:
                    stack.append((limit, in_cluster))
                    fp.seek(element.data_start)
                    stack.append((element.data_end, True))
                    break
                if in_cluster and element.element_id in {MATROSKA_SIMPLE_BLOCK_ID, MATROSKA_BLOCK_ID}:
                    fp.seek(element.data_start)
                    block_data = fp.read(element.data_end - element.data_start)
                    track_number, frames = parse_matroska_block_frames(block_data)
                    if not av1_tracks or track_number in av1_tracks:
                        for frame in frames:
                            yield index, frame, 0
                            index += 1
                elif element.element_id in MATROSKA_CONTAINER_IDS and element.element_id != MATROSKA_BLOCK_GROUP_ID:
                    stack.append((limit, in_cluster))
                    fp.seek(element.data_start)
                    stack.append((element.data_end, in_cluster))
                    break
                elif in_cluster and element.element_id == MATROSKA_BLOCK_GROUP_ID:
                    stack.append((limit, in_cluster))
                    fp.seek(element.data_start)
                    stack.append((element.data_end, True))
                    break
                fp.seek(element.data_end)

def iter_mkv_samples(path: str) -> Iterator[Tuple[int, bytes, int]]:
    yield from iter_matroska_block_payloads(path)

def detect_input_format(path: str) -> str:
    with open(path, "rb") as fp:
        head = fp.read(16)
    if head.startswith(b"DKIF"):
        return "ivf"
    if head.startswith(bytes.fromhex("1a45dfa3")):
        return "mkv"
    if len(head) >= 8 and head[4:8] in {b"ftyp", b"moov", b"moof", b"free", b"wide", b"mdat"}:
        return "mp4"
    raise ValueError("Unsupported input format. Expected AV1 IVF, AV1 MP4/fMP4, or AV1 MKV.")

def output_path_for_input(path: str) -> str:
    base, _ext = os.path.splitext(path)
    return base + ".json"

def build_iterator(path: str, input_format: str) -> Iterator[Tuple[int, bytes, int]]:
    if input_format == "ivf":
        return iter_ivf_samples(path)
    if input_format == "mp4":
        return iter_mp4_samples(path)
    if input_format == "mkv":
        return iter_mkv_samples(path)
    raise ValueError("Unsupported input format")

def extract(path: str, output: str) -> None:
    input_format = detect_input_format(path)
    iterator = build_iterator(path, input_format)
    t35_by_frame: List[bytes] = []
    scanned = 0
    total_hint = os.path.getsize(path) if input_format == "mkv" else 0
    status(f"Input format: {input_format.upper()}")
    status("Scanning AV1 samples for HDR10+ metadata...")
    last_progress = -1.0
    last_file_pos = 0
    for index, payload, total in iterator:
        scanned += 1
        t35_by_frame.append(extract_t35_from_av1_sample(payload, index))
        if total > 0:
            current_progress = scanned * 100.0 / total
        elif input_format == "mkv" and total_hint > 0:
            current_progress = min(99.0, last_file_pos * 100.0 / total_hint)
        else:
            current_progress = min(99.0, (scanned % 1000) / 10.0)
        if current_progress >= 100.0 or current_progress - last_progress >= 0.10:
            progress_bar(current_progress)
            last_progress = current_progress
        last_file_pos = min(total_hint, last_file_pos + len(payload))
    if scanned == 0:
        raise ValueError("No AV1 samples were found")
    progress_done()
    status("Reordering metadata... Done.")
    status("Reading parsed dynamic metadata... Done.")
    classic = build_classic_json_from_t35_list(t35_by_frame)
    status("Generating and writing metadata to JSON file...")
    with open(output, "w", encoding="utf-8") as fp:
        json.dump(classic, fp, indent=2)
    status("Generating and writing metadata to JSON file... Done.")
    status(f"Frames written: {len(t35_by_frame)}")
    status(f"Output: {output}")

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pyav1plus.py", description="Extract HDR10+ metadata from AV1 IVF, MP4/fMP4, or MKV.")
    parser.add_argument("input", help="Input .ivf, .mp4, .m4v, .mkv, or .webm file")
    parser.add_argument("-o", "--output", help="Output JSON path. Default: input basename with .json")
    return parser

def main() -> None:
    args = build_parser().parse_args()
    output = args.output or output_path_for_input(args.input)
    try:
        extract(args.input, output)
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted.\n")
        raise SystemExit(130)
    except Exception as exc:
        sys.stderr.write(f"\nERROR: {exc}\n")
        raise SystemExit(1)

if __name__ == "__main__":
    main()