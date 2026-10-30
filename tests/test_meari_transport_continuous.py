"""Continuous worker framing, random access, deadline and cleanup regressions."""
from __future__ import annotations

import io
import json
import socket
import struct
import subprocess
import sys
import textwrap
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_meari_transport_worker import WORKER, WORKER_PATH, H264, HEVC, TimeoutSocket, context


def frame(payload, sequence=1, *, iframe=True, timestamp=0):
    header = bytearray(48 if iframe else 40)
    header[:4] = b"\x00\x00\x01" + bytes([0xFC if iframe else 0xFD])
    struct.pack_into("<I", header, 0x10 if iframe else 0x08, sequence)
    struct.pack_into("<I", header, len(header) - 12, timestamp)
    return bytes(header) + payload


def records(payload):
    rows = []
    while payload:
        if len(payload) < 5:
            raise AssertionError("truncated record header")
        kind, length = payload[:1], struct.unpack(">I", payload[1:5])[0]
        if not 0 < length <= WORKER.MAX_STREAM_FRAME_BYTES or len(payload) < 5 + length:
            raise AssertionError("invalid record length")
        rows.append((kind, payload[5:5 + length]))
        payload = payload[5 + length:]
    return rows


class FramingTests(unittest.TestCase):
    def test_binary_framing_keeps_annexb_unmodified(self):
        output = io.BytesIO()
        writer = WORKER.BinaryOutput(output)
        writer.write(b"J", b'{"codec":"hevc"}')
        writer.write(b"V", HEVC)
        self.assertEqual(records(output.getvalue()), [(b"J", b'{"codec":"hevc"}'), (b"V", HEVC)])

    def test_record_lengths_and_kinds_are_bounded_before_write(self):
        output = io.BytesIO()
        writer = WORKER.BinaryOutput(output)
        for kind, payload in ((b"X", b"x"), (b"V", b""), (b"J", b"x" * 4097), (b"V", b"x" * (WORKER.MAX_STREAM_FRAME_BYTES + 1))):
            with self.subTest(kind=kind, size=len(payload)), self.assertRaises(ValueError):
                writer.write(kind, payload)
        self.assertEqual(output.getvalue(), b"")

    def test_closed_consumer_raises_owned_stop_signal(self):
        output = io.BytesIO()
        output.close()
        with self.assertRaises(WORKER.OutputClosed):
            WORKER.BinaryOutput(output).write(b"V", H264)

    def test_short_pipe_writes_cannot_truncate_video_records(self):
        class ShortOutput(io.BytesIO):
            def write(self, value):
                return super().write(value[:3])

        output = ShortOutput()
        WORKER.BinaryOutput(output).write(b"V", HEVC)
        self.assertEqual(records(output.getvalue()), [(b"V", HEVC)])

    def test_stream_cli_never_mixes_lines_or_private_input_into_records(self):
        result = subprocess.run([sys.executable, "-I", "-B", "-u", str(WORKER_PATH), "--stream"], input=b'{"PRIVATE":"PRIVATE-HOSTKEY"}', capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, b"")
        parsed = records(result.stdout)
        self.assertEqual([kind for kind, _ in parsed], [b"J", b"J"])
        statuses = [json.loads(data) for _, data in parsed]
        self.assertEqual([row["probe_stage"] for row in statuses], ["worker_started", "invalid_context"])
        self.assertTrue(statuses[-1]["final"])
        self.assertNotIn(b"PRIVATE", result.stdout)

    def test_stream_cli_orders_codec_status_before_video_and_final_status(self):
        program = textwrap.dedent(f"""
            import importlib.util, sys
            spec = importlib.util.spec_from_file_location('owned_test_worker', {str(WORKER_PATH)!r})
            worker = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(worker)
            def receive(context, emit, *, frame_output=None):
                emit('receiving_video')
                summary = {{'codec':'hevc','video_frames':2,'video_bytes':50,'received_packets':3,'keyframe_received':True,'parameter_sets_received':True,'decoded':False}}
                frame_output({HEVC!r}, summary)
                frame_output({HEVC!r}, summary)
                return 'stream_complete', summary
            worker.run_probe = receive
            sys.argv = [{str(WORKER_PATH)!r}, '--stream']
            raise SystemExit(worker.main())
        """)
        result = subprocess.run([sys.executable, "-I", "-B", "-u", "-c", program], input=json.dumps(context()).encode(), capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        parsed = records(result.stdout)
        self.assertEqual([kind for kind, _ in parsed], [b"J", b"J", b"J", b"V", b"V", b"J"])
        first = json.loads(parsed[2][1])
        self.assertEqual(first["probe_stage"], "video_keyframe_received")
        self.assertEqual(first["codec"], "hevc")
        self.assertFalse(first["final"])
        self.assertFalse(first["decoded"])
        self.assertTrue(json.loads(parsed[-1][1])["final"])
        self.assertEqual(parsed[3][1], HEVC)
        self.assertEqual(result.stderr, b"")
        self.assertNotIn(b"PRIVATE", result.stdout)


class AccessUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = WORKER.load_protocol()

    def setUp(self):
        self.lib.budget.configure(30)
        self.output = []
        self.observation = WORKER.Observation(self.lib, lambda data, summary: self.output.append((data, summary)))

    def test_h264_first_output_requires_parameters_and_random_access(self):
        pframe = b"\x00\x00\x01\x41\x9a\x80"
        self.observation.feed(frame(pframe, 1, iframe=False))
        self.observation.feed(frame(b"\x00\x00\x01\x65\x88\x80", 2))
        self.assertEqual(self.output, [])
        self.observation.feed(frame(H264, 3))
        self.observation.feed(frame(pframe, 4, iframe=False))
        self.assertEqual([data for data, _ in self.output], [H264, pframe])
        self.assertEqual(self.output[0][1]["codec"], "h264")
        self.assertFalse(self.output[0][1]["decoded"])

    def test_hevc_separate_parameter_sets_are_prefixed_to_later_keyframe(self):
        units = self.lib.codecs.annexb_units(HEVC)
        for index, unit in enumerate(units[:3]):
            self.observation.feed(frame(unit, index + 1))
        self.assertEqual(self.output, [])
        self.observation.feed(frame(units[3], 4))
        self.assertEqual(len(self.output), 1)
        output = self.output[0][0]
        self.assertEqual(set(self.lib.codecs.annexb_units(output)), set(units))
        self.assertEqual(self.output[0][1]["codec"], "hevc")

    def test_continuous_media_exceeds_probe_frame_and_byte_caps(self):
        self.observation.feed(frame(H264))
        self.observation.video_frames = 256
        self.observation.video_bytes = WORKER.MAX_MEDIA_BYTES
        for index in range(2, 302):
            self.observation.feed(frame(b"\x00\x00\x01\x41\x9a\x80", index, iframe=False))
        self.assertEqual(len(self.output), 301)
        self.assertGreater(self.observation.video_bytes, WORKER.MAX_MEDIA_BYTES)
        self.assertIsNotNone(self.observation.last_frame_at)

    def test_duplicate_backward_pictures_and_parameter_only_frames_are_not_output(self):
        self.observation.feed(frame(H264, 10))
        pframe = b"\x00\x00\x01\x41\x9a\x80"
        for sequence in (11, 11, 9, 12):
            self.observation.feed(frame(pframe, sequence, iframe=False))
        self.observation.feed(frame(self.lib.codecs.annexb_units(H264)[0], 13))
        self.assertEqual(len(self.output), 3)

    def test_codec_switch_terminates_decoder_epoch(self):
        self.observation.feed(frame(H264))
        with self.assertRaisesRegex(ValueError, "codec_changed"):
            self.observation.feed(frame(HEVC, 2))
        self.assertEqual(len(self.output), 1)

    def test_encrypted_compact_and_extended_iframes_and_pframes_retain_annexb(self):
        from Crypto.Cipher import DES3

        def encrypted(payload, sequence, *, iframe, extended):
            offset = 48 if iframe else 40
            data = bytearray(frame(payload, sequence, iframe=iframe))
            if extended:
                data[offset:offset] = b"\x00" * 12
                struct.pack_into("<I", data, offset + 8, len(payload))
            length = min(128, (len(data) - offset) // 8 * 8)
            key = self.lib.protocol.STREAM_ENCRYPT_KEY[:24].ljust(24, b"\x00")
            cipher = DES3.new(key, DES3.MODE_ECB)
            data[offset:offset + length] = cipher.encrypt(bytes(data[offset:offset + length]))
            return bytes(data)

        for codec, keyframe, delta in (
            ("h264", H264, b"\x00\x00\x01\x41\x9a" + b"\x80" * 160),
            ("hevc", HEVC, b"\x00\x00\x01\x02\x01" + b"\x80" * 160),
        ):
            for extended in (False, True):
                with self.subTest(codec=codec, extended=extended):
                    output = []
                    observation = WORKER.Observation(self.lib, lambda data, info: output.append(data))
                    observation.feed(encrypted(keyframe, 1, iframe=True, extended=extended))
                    observation.feed(encrypted(delta, 2, iframe=False, extended=extended))
                    self.assertEqual(observation.codec, codec)
                    self.assertEqual(output, [keyframe, delta])


class ContinuousTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = WORKER.load_protocol()

    def transport(self, *, frames=3, packet_schedule=None):
        from vendor.kcp_tunnel import build_iva_data_frame
        instances, sent, clock = [], [], [0.0]

        class Signaling:
            heartbeat_interval_s = 30

            def __init__(self, *args, **kwargs):
                self.sock = TimeoutSocket()
                self.closed = False
                instances.append(self)

            def connect(self, **kwargs): pass
            def register(self, **kwargs): pass
            def webrtc_hello_full(self): pass
            def query_device_status(self, uuid): return {"status": "online"}
            def request_coturn(self, uuid): return {"coturn_ip": "192.0.2.1", "coturn_port": 9100, "username": "PRIVATE", "pwd": "PRIVATE"}
            def send_offer(self, uuid, sdp): return {"sdp": "a=ice-ufrag:camera\na=ice-pwd:private\nm=audio 9101 RTP / AVP 0\nc=IN IP4 192.0.2.2\n"}
            def recv_webrtc_content(self): raise socket.timeout()
            def send_candidate_complete(self, uuid): return {}
            def send_heartbeat(self, **kwargs): pass
            def close(self): self.closed = True

        class Turn:
            relay_ip, relay_port = "192.0.2.3", 9102

            def __init__(self, *args):
                self.closed = False
                self.refresh_count = 0
                instances.append(self)

            def connect(self): pass
            def allocate(self): return True
            def create_permission(self, ip): return True
            def channel_bind(self, ip, port): return True
            def refresh(self):
                self.refresh_count += 1
                return True
            def send_to_peer(self, ip, port, data): sent.append(data)
            def send_ice_binding(self, *args): pass
            def close(self): self.closed = True

        fake = SimpleNamespace(**vars(self.lib))
        fake.MsgSvrClient, fake.TurnClient = Signaling, Turn
        fake.discover_msgsvr_endpoints = lambda **kwargs: [("192.0.2.1", 28974)]
        count = [0]

        def receive(*args, **kwargs):
            clock[0] += 1
            count[0] += 1
            if packet_schedule is not None:
                data = packet_schedule.get(count[0])
                if data is None:
                    return []
                return [SimpleNamespace(data=data, peer=("192.0.2.2", 9101), source=("192.0.2.1", 9100), via_turn=True, stun=None)]
            if count[0] > frames:
                return []
            payload = H264 if count[0] == 1 else b"\x00\x00\x01\x41\x9a\x80"
            data = build_iva_data_frame(frame(payload, count[0], iframe=count[0] == 1))
            return [SimpleNamespace(data=data, peer=("192.0.2.2", 9101), source=("192.0.2.1", 9100), via_turn=True, stun=None)]

        fake.recv_peer_packets = receive
        return fake, clock, instances, sent

    def test_persistent_kcp_loss_resumes_only_at_verified_keyframe_without_new_packets(self):
        from vendor.kcp_tunnel import build_iva_data_frame, build_kcp_segment, KCP_CMD_PUSH, parse_kcp_segments, KCP_CMD_ACK
        codecs = ((H264, b"\x00\x00\x01\x41\x9a\x80"),
                  (HEVC, b"\x00\x00\x01\x02\x01\x80"))

        def packet(sn, payload, *, frg=0):
            return build_kcp_segment(KCP_CMD_PUSH, sn=sn, frg=frg, data=payload)

        for keyframe, delta, after_first_frame in (
            (keyframe, delta, started)
            for keyframe, delta in codecs for started in (False, True)
        ):
            with self.subTest(codec="h264" if keyframe == H264 else "hevc", after_first_frame=after_first_frame):
                interrupted = build_iva_data_frame(frame(delta, 2, iframe=False))
                schedule = {
                    1: packet(0, build_iva_data_frame(frame(keyframe if after_first_frame else delta, 1, iframe=after_first_frame))),
                    2: packet(1, interrupted[:24], frg=2),
                    # SN 2 is permanently lost. The sender fills its window,
                    # then sends nothing further until cumulative UNA advances.
                    3: (packet(3, interrupted[-8:])
                        + packet(4, build_iva_data_frame(frame(delta, 3, iframe=False)))
                        + packet(5, build_iva_data_frame(frame(delta, 4, iframe=True)))
                        + packet(6, build_iva_data_frame(frame(keyframe, 5)))
                        + packet(7, build_iva_data_frame(frame(delta, 6, iframe=False)))),
                }
                fake, clock, instances, sent = self.transport(packet_schedule=schedule)
                output = []
                raw = context()
                raw["timeout_seconds"] = 15
                with patch.object(WORKER.time, "monotonic", side_effect=lambda: clock[0]):
                    stage, _ = WORKER.run_probe(raw, lambda stage: None, library=fake, frame_output=lambda data, info: output.append((clock[0], data)))
                self.assertEqual(stage, "stream_idle_timeout")
                self.assertEqual([data for _, data in output], ([keyframe] if after_first_frame else []) + [keyframe, delta])
                self.assertGreaterEqual(output[-1][0], 5)
                self.assertLess(output[-1][0], 10)
                acknowledgements = [segment for raw_packet in sent for segment in parse_kcp_segments(raw_packet) if segment["cmd"] == KCP_CMD_ACK]
                self.assertTrue(any(segment["una"] == 8 for segment in acknowledgements))
                self.assertTrue(all(item.closed for item in instances))

    def test_reordered_kcp_packet_is_retransmitted_without_discarding_video(self):
        from vendor.kcp_tunnel import build_iva_data_frame, build_kcp_segment, KCP_CMD_PUSH
        delta = b"\x00\x00\x01\x41\x9a\x80"

        def packet(sn, payload, *, iframe=False):
            return build_kcp_segment(KCP_CMD_PUSH, sn=sn, data=build_iva_data_frame(frame(payload, sn + 1, iframe=iframe)))

        fake, clock, _, _ = self.transport(packet_schedule={1: packet(0, H264, iframe=True), 2: packet(2, delta), 3: packet(1, delta)})
        output = []
        with patch.object(WORKER.time, "monotonic", side_effect=lambda: clock[0]):
            stage, _ = WORKER.run_probe(context(), lambda stage: None, library=fake, frame_output=lambda data, info: output.append(data))
        self.assertEqual(stage, "stream_idle_timeout")
        self.assertEqual(output, [H264, delta, delta])

    def test_recording_connections_never_skip_kcp_gaps(self):
        from vendor.kcp_tunnel import build_iva_data_frame, build_kcp_segment, KCP_CMD_PUSH
        schedule = {
            1: build_kcp_segment(KCP_CMD_PUSH, sn=0, data=build_iva_data_frame(frame(H264))),
            2: build_kcp_segment(KCP_CMD_PUSH, sn=2, data=build_iva_data_frame(frame(H264, 2))),
        }
        for operation, date in (("month", "202610"), ("day", "20261030"), ("playback", "20261030090000")):
            with self.subTest(operation=operation):
                fake, clock, _, _ = self.transport(packet_schedule=schedule)
                raw = {**context(), "hdd": {"operation": operation, "date": date}}
                with patch.object(WORKER.time, "monotonic", side_effect=lambda: clock[0]), patch.object(fake.KcpTunnel, "skip_gap", side_effect=AssertionError("recording data must remain lossless")) as skip:
                    WORKER.run_probe(raw, lambda stage: None, library=fake, frame_output=lambda data, info: self.fail("unacknowledged recording"))
                skip.assert_not_called()

    def test_continuous_transport_keeps_receiving_after_first_frame_then_stops_idle(self):
        fake, clock, instances, sent = self.transport(frames=3)
        output = []
        raw = context()
        raw["timeout_seconds"] = 5
        with patch.object(WORKER.time, "monotonic", side_effect=lambda: clock[0]):
            stage, summary = WORKER.run_probe(raw, lambda stage: None, library=fake, frame_output=lambda data, info: output.append(data))
        self.assertEqual(stage, "stream_idle_timeout")
        self.assertEqual(len(output), 3)
        self.assertGreater(clock[0], raw["timeout_seconds"])
        self.assertTrue(summary["keyframe_received"])
        self.assertTrue(all(item.closed for item in instances))
        self.assertGreaterEqual(len(sent), 3)  # start, heartbeat and stop

    def test_stream_lifetime_is_bounded_even_with_continuous_frames(self):
        fake, clock, instances, _ = self.transport(frames=100)
        output = []
        with patch.object(WORKER.time, "monotonic", side_effect=lambda: clock[0]), patch.object(WORKER, "MAX_STREAM_SECONDS", 8):
            stage, _ = WORKER.run_probe(context(), lambda stage: None, library=fake, frame_output=lambda data, info: output.append(data))
        self.assertEqual(stage, "stream_complete")
        self.assertGreater(len(output), 1)
        self.assertLessEqual(clock[0], 7)
        self.assertTrue(all(item.closed for item in instances))

    def test_first_frame_still_obeys_short_startup_timeout(self):
        fake, clock, instances, _ = self.transport(frames=0)
        raw = context()
        raw["timeout_seconds"] = 5
        with patch.object(WORKER.time, "monotonic", side_effect=lambda: clock[0]):
            stage, _ = WORKER.run_probe(raw, lambda stage: None, library=fake, frame_output=lambda data, info: self.fail("unexpected video"))
        self.assertEqual(stage, "no_video_frame")
        self.assertLessEqual(clock[0], 4)
        self.assertTrue(all(item.closed for item in instances))

    def test_long_stream_renews_turn_allocation_and_channel_permission(self):
        fake, clock, instances, _ = self.transport(frames=130)
        with patch.object(WORKER.time, "monotonic", side_effect=lambda: clock[0]):
            stage, _ = WORKER.run_probe(context(), lambda stage: None, library=fake, frame_output=lambda data, info: None)
        self.assertEqual(stage, "stream_idle_timeout")
        self.assertEqual(instances[1].refresh_count, 1)
        self.assertTrue(all(item.closed for item in instances))

    def test_closed_stdout_cleans_transport_without_reconnecting(self):
        fake, clock, instances, _ = self.transport(frames=100)

        def consume(data, summary):
            raise WORKER.OutputClosed()

        with patch.object(WORKER.time, "monotonic", side_effect=lambda: clock[0]), self.assertRaises(WORKER.OutputClosed):
            WORKER.run_probe(context(), lambda stage: None, library=fake, frame_output=consume)
        self.assertEqual(len(instances), 2)
        self.assertTrue(all(item.closed for item in instances))


if __name__ == "__main__":
    unittest.main()
