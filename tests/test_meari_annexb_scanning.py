"""Pin media boundaries while replacing Python byte scans with native searches.

The small reference scanners preserve the pre-optimization behavior, including
its different treatment of terminal empty three- and four-byte start codes.
Performance is measured separately; these tests do not depend on machine speed.
"""
from __future__ import annotations

import importlib
import itertools
import random
import unittest

from test_meari_transport_continuous import frame
from test_meari_transport_worker import H264, HEVC, WORKER


def reference_headers(data):
    """Frozen byte-wise decoder-header scanner from 2.0.0b3."""
    result = []
    i = 0
    while i < len(data) - 3:
        if data[i] == 0 and data[i + 1] == 0:
            if data[i + 2] == 1:
                start = i + 3
            elif data[i + 2] == 0 and data[i + 3] == 1:
                start = i + 4
            else:
                i += 1
                continue
            if start < len(data):
                result.append(start)
                i = start
                continue
        i += 1
    return result


def reference_units(data):
    """Frozen NAL slicing boundaries; intentionally retains empty 4-byte NALs."""
    starts = []
    i = 0
    while i + 3 < len(data):
        if data[i] == 0 and data[i + 1] == 0:
            if data[i + 2] == 1:
                starts.append(i)
                i += 3
                continue
            if data[i + 2] == 0 and data[i + 3] == 1:
                starts.append(i)
                i += 4
                continue
        i += 1
    return [
        data[start:starts[index + 1] if index + 1 < len(starts) else len(data)]
        for index, start in enumerate(starts)
    ]


def reference_stream_start(data, start=0):
    """Frozen vendor frame search: only F9, FA, FC and FD are frame markers."""
    i = max(0, start)
    while i + 3 < len(data):
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
            if data[i + 3] in (0xF9, 0xFA, 0xFC, 0xFD):
                return i
        i += 1
    return -1


class AnnexBScanningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = WORKER.load_protocol()
        cls.base = importlib.import_module(cls.lib.codecs.__name__ + ".base")

    def assert_scanners_match(self, payload):
        self.assertEqual(list(self.base.iter_annexb_headers(payload)), reference_headers(payload))
        self.assertEqual(self.lib.codecs.annexb_units(payload), reference_units(payload))

    def test_empty_terminal_start_codes_and_leading_zero_boundaries(self):
        three, four = b"\x00\x00\x01", b"\x00\x00\x00\x01"
        cases = (
            (b"", [], []),
            (three, [], []),
            (four, [], [four]),
            (three + b"A" + three, [3], [three + b"A" + three]),
            (three + b"A" + four, [3], [three + b"A", four]),
            (three + three + b"A", [3, 6], [three, three + b"A"]),
            (b"\x00" * 5 + b"\x01A", [6], [four + b"A"]),
            (b"noise" + four + b"A" + three + b"B", [9, 13], [four + b"A", three + b"B"]),
        )
        for payload, headers, units in cases:
            with self.subTest(payload=payload):
                self.assertEqual(list(self.base.iter_annexb_headers(payload)), headers)
                self.assertEqual(self.lib.codecs.annexb_units(payload), units)

    def test_exhaustive_short_zero_one_and_noise_inputs_match_old_scans(self):
        # All short inputs expose overlap and off-by-one cases without relying
        # on a hand-selected collection of valid bitstreams.
        for size in range(9):
            for values in itertools.product((0, 1, 2), repeat=size):
                self.assert_scanners_match(bytes(values))

    def test_seeded_mixed_markers_and_long_payloads_match_old_scans(self):
        rng = random.Random(20261028)
        tokens = (
            b"\x00\x00\x01", b"\x00\x00\x00\x01", b"\x00" * 13,
            b"\x00\x00\x03\x01", H264, HEVC,
            b"\x00\x00\x01\xfc", b"\x00\x00\x01\xfe",
        )
        for case in range(200):
            chunks = []
            for _ in range(rng.randrange(1, 25)):
                chunks.append(rng.choice(tokens))
                chunks.append(rng.randbytes(rng.randrange(100)))
            payload = b"".join(chunks)
            with self.subTest(case=case):
                self.assert_scanners_match(payload)
                for start in (-20, 0, 1, len(payload) // 2, len(payload) - 3, len(payload), len(payload) + 20):
                    self.assertEqual(self.lib.codec._find_stream_start(payload, start), reference_stream_start(payload, start))
        for payload in (b"X" * 262144, b"\x00" * 262144, H264 + b"X" * 262144 + HEVC):
            self.assert_scanners_match(payload)
            self.assertEqual(self.lib.codec._find_stream_start(payload), reference_stream_start(payload))

    def test_vendor_search_rejects_nal_headers_and_honors_every_start_offset(self):
        markers = tuple(b"\x00\x00\x01" + bytes([kind]) for kind in range(256))
        for marker in markers:
            payload = b"lead\x00" + marker + b"body\x00\x00\x01\xfd"
            for start in range(-2, len(payload) + 3):
                self.assertEqual(self.lib.codec._find_stream_start(payload, start), reference_stream_start(payload, start))
        for size in range(4):
            payload = b"\x00\x00\x01"[:size]
            self.assertEqual(self.lib.codec._find_stream_start(payload), -1)

    def test_mutable_input_is_not_modified_and_retains_slice_types(self):
        payload = bytearray(b"lead" + H264 + b"\x00\x00\x00\x01")
        before = payload[:]
        self.assert_scanners_match(payload)
        self.assertTrue(all(isinstance(unit, bytearray) for unit in self.lib.codecs.annexb_units(payload)))
        self.assertEqual(payload, before)

    def test_live_observation_preserves_mixed_start_codes_and_escaped_payload(self):
        self.lib.budget.configure(30)
        output = []
        observation = WORKER.Observation(self.lib, lambda data, summary: output.append((data, summary)))
        originals = reference_units(HEVC)
        units = [(b"\x00\x00\x01" if index % 2 else b"\x00\x00\x00\x01") + self.lib.codecs.nal_payload(unit)
                 for index, unit in enumerate(originals)]
        units[-1] += b"\xaa\x00\x00\x03\x01\x55" * 1024
        payload = b"".join(units)
        observation.feed(frame(payload, 1))
        delta = b"\x00\x00\x01\x02\x01\x80\x00\x00\x03\x01\x88"
        observation.feed(frame(delta, 2, iframe=False))
        self.assertEqual([data for data, _ in output], [payload, delta])
        self.assertEqual(output[0][1]["codec"], "hevc")
        self.assertTrue(output[0][1]["keyframe_received"])
        self.assertTrue(output[0][1]["parameter_sets_received"])
        self.assertFalse(output[0][1]["decoded"])

    def test_parameter_cache_keeps_original_nal_bytes_before_later_keyframe(self):
        self.lib.budget.configure(30)
        output = []
        observation = WORKER.Observation(self.lib, lambda data, summary: output.append(data))
        units = reference_units(HEVC)
        units[1] = b"\x00\x00\x01" + self.lib.codecs.nal_payload(units[1])
        for index, unit in enumerate(units[:3]):
            observation.feed(frame(unit, index + 1))
        self.assertEqual(output, [])
        observation.feed(frame(units[-1], 4))
        self.assertEqual(len(output), 1)
        self.assertEqual(set(reference_units(output[0])), set(units))
        self.assertTrue(output[0].endswith(units[-1]))


if __name__ == "__main__":
    unittest.main()
