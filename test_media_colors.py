import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

import media_convert_5 as converter


def video_info(**overrides):
    values = dict(
        color_primaries='bt709', color_transfer='bt709',
        color_space='bt709', color_range='tv', codec_name='hevc',
        width=1920, height=1080,
    )
    values.update(overrides)
    return converter.VideoInfo(**values)


def command(info, fallback=False):
    with patch.object(converter, 'stream_mapping_args', return_value=[]):
        return converter.build_cmd('input.mkv', 'output.mkv', fallback, info)


class ColorProbeTests(unittest.TestCase):
    def probe(self, stream, frames):
        result = subprocess.CompletedProcess(
            [], 0, json.dumps({'streams': [stream], 'frames': frames}), '',
        )
        with patch.object(converter.subprocess, 'run', return_value=result) as run:
            info = converter.probe_video_info('input.mkv')
        args = run.call_args.args[0]
        self.assertEqual(args[args.index('-read_intervals') + 1], '%+#60')
        return info

    def test_uses_frame_properties_for_hdr_detection(self):
        info = self.probe(
            {'codec_name': 'hevc', 'width': 3840, 'height': 2160,
             'color_transfer': 'unknown', 'color_range': 'pc'},
            [{}, {'color_transfer': 'smpte2084', 'color_primaries': 'bt2020',
                  'color_space': 'bt2020nc'}],
        )
        self.assertEqual(converter.hdr_mode(info), converter.HDR10_PQ)
        self.assertEqual(info.color_range, 'pc')
        self.assertEqual((info.width, info.height), (3840, 2160))
        self.assertEqual(info.color_primaries, 'bt2020')

    def test_stream_values_survive_missing_frame_fields(self):
        info = self.probe(
            {'color_space': 'smpte170m', 'color_range': 'pc'},
            [{'color_space': 'unknown', 'color_range': None}],
        )
        self.assertEqual(info.color_space, 'smpte170m')
        self.assertEqual(info.color_range, 'pc')

    def test_frame_conflict_is_logged_and_decoded_value_is_used(self):
        with self.assertLogs(converter.__name__, level='WARNING') as logs:
            info = self.probe(
                {'color_range': 'tv'}, [{'color_range': 'pc'}],
            )
        self.assertEqual(info.color_range, 'pc')
        self.assertIn('disagreement', logs.output[0])

    def test_changing_frame_properties_cannot_be_flattened_silently(self):
        with self.assertRaisesRegex(converter.UnsupportedVideoError, 'Changing color_space'):
            self.probe({}, [{'color_space': 'bt709'}, {'color_space': 'smpte170m'}])


class ColorCommandTests(unittest.TestCase):
    def test_preserves_known_properties_in_both_decode_paths(self):
        cases = [
            ('smpte170m', 'smpte170m', 'smpte170m', 'pc', (6, 6, 6, 1)),
            ('bt470bg', 'gamma28', 'bt470bg', 'tv', (5, 5, 5, 0)),
            ('bt709', 'bt709', 'bt709', 'pc', (1, 1, 1, 1)),
            ('smpte432', 'smpte2084', 'bt2020nc', 'pc', (12, 16, 9, 1)),
            ('bt2020', 'arib-std-b67', 'bt2020nc', 'tv', (9, 18, 9, 0)),
        ]
        for primaries, transfer, matrix, color_range, codes in cases:
            for fallback in (False, True):
                with self.subTest(colors=(primaries, transfer, matrix, color_range), fallback=fallback):
                    info = video_info(color_primaries=primaries, color_transfer=transfer,
                                      color_space=matrix, color_range=color_range)
                    cmd = command(info, fallback)
                    for flag, value in [('-color_primaries', primaries), ('-color_trc', transfer),
                                        ('-colorspace', matrix), ('-color_range', color_range)]:
                        self.assertEqual(cmd[cmd.index(flag) + 1], value)
                    vf = cmd[cmd.index('-vf:v:0') + 1]
                    self.assertTrue(vf.startswith(
                        f'setparams=color_primaries={codes[0]}:color_trc={codes[1]}:'
                        f'colorspace={codes[2]}:range={color_range},'))
                    self.assertIn(f'out_range={color_range}', vf)
                    mode = converter.hdr_mode(info)
                    if converter.output_codec(mode) == 'av1':  # SDR
                        expected = (f'av1_metadata=color_primaries={codes[0]}:transfer_characteristics={codes[1]}:'
                                    f'matrix_coefficients={codes[2]}:color_range={codes[3]}')
                        encoder = 'av1_qsv'
                    else:  # HDR stays HEVC
                        expected = (f'hevc_metadata=colour_primaries={codes[0]}:transfer_characteristics={codes[1]}:'
                                    f'matrix_coefficients={codes[2]}:video_full_range_flag={codes[3]}')
                        encoder = 'hevc_qsv'
                    self.assertEqual(cmd[cmd.index('-bsf:v:0') + 1], expected)
                    self.assertEqual(cmd[cmd.index('-c:v:0') + 1], encoder)

    def test_defaults_fill_only_missing_fields_and_are_logged(self):
        info = video_info(color_primaries='smpte432', color_transfer='unknown',
                          color_space='', color_range='pc')
        with self.assertLogs(converter.__name__, level='WARNING') as logs:
            cmd = command(info)
        self.assertEqual(cmd[cmd.index('-color_primaries') + 1], 'smpte432')
        self.assertEqual(cmd[cmd.index('-color_range') + 1], 'pc')
        self.assertEqual(cmd[cmd.index('-color_trc') + 1], 'bt709')
        self.assertIn('color_transfer=bt709', logs.output[0])
        self.assertNotIn('color_range=', logs.output[0])

    def test_sd_defaults_use_source_dimensions_without_overriding_known_colors(self):
        for height, standard in [(480, 'smpte170m'), (486, 'smpte170m'), (576, 'bt470bg')]:
            with self.subTest(height=height):
                info = video_info(width=720, height=height, color_primaries='',
                                  color_transfer='', color_space='', color_range='')
                colors = converter.resolve_color_properties(info)
                self.assertEqual(colors['color_space'], standard)
                self.assertEqual(colors['color_primaries'], standard)
                self.assertEqual(colors['color_transfer'], 'smpte170m')
                tagged = replace(info, color_primaries='bt709', color_space='bt709')
                self.assertEqual(converter.resolve_color_properties(tagged)['color_space'], 'bt709')

    def test_profile_five_sets_hdr10_properties_after_rendering(self):
        info = video_info(dovi_profile=5, dovi_bl_present=True,
                          color_space='ipt-c2', color_range='pc')
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                cmd = command(info, fallback)
                vf = cmd[cmd.index('-vf:v:0') + 1]
                self.assertGreater(vf.index('setparams='), vf.index('libplacebo='))
                self.assertEqual(cmd[cmd.index('-color_trc') + 1], 'smpte2084')
                self.assertEqual(cmd[cmd.index('-color_primaries') + 1], 'bt2020')
                self.assertEqual(cmd[cmd.index('-color_range') + 1], 'tv')

    def test_unsupported_known_values_are_not_replaced_with_defaults(self):
        for info in [video_info(color_transfer='vlog'), video_info(color_space='gbr')]:
            with self.subTest(info=info):
                with self.assertRaises(converter.UnsupportedVideoError):
                    command(info)


class ColorValidationTests(unittest.TestCase):
    def validate(self, source, output):
        with tempfile.NamedTemporaryFile() as f:
            f.write(b'nonempty')
            f.flush()
            with patch.object(converter, 'probe_video_info', return_value=output):
                return converter.validate_output(f.name, converter.hdr_mode(source), source)

    def test_accepts_preserved_nonstandard_sdr_and_hdr_properties(self):
        for source in [video_info(color_primaries='bt470bg', color_space='bt470bg', color_range='pc'),
                       video_info(color_primaries='smpte432', color_transfer='smpte2084', color_space='bt2020nc')]:
            with self.subTest(source=source):
                expected_codec = converter.output_codec(converter.hdr_mode(source))  # SDR -> av1, HDR -> hevc
                output = replace(source, codec_name=expected_codec)
                self.assertEqual(self.validate(source, output), output)
                wrong = replace(source, codec_name='hevc' if expected_codec == 'av1' else 'av1')
                with self.assertRaisesRegex(RuntimeError, 'expected'):
                    self.validate(source, wrong)

    def test_rejects_wrong_color_or_range_before_installing_output(self):
        source = video_info(color_space='smpte170m', color_range='pc')
        for change in [{'color_space': 'bt709'}, {'color_range': 'tv'}, {'color_transfer': ''}]:
            with self.subTest(change=change):
                with self.assertRaisesRegex(RuntimeError, 'incorrect color signaling'):
                    self.validate(source, replace(source, codec_name='av1', **change))


if __name__ == '__main__':
    unittest.main()
