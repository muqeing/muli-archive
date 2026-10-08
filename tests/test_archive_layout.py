import unittest
from muli_sorter.archive_io import ArchiveError
from muli_sorter.archive_layout import video_route, studio_target_rows


def unit(device, names, kind='video'):
    return {'unit_id':'unit-fixture','kind':kind,'device':device,
            'files':[{'source_path':'BATCH/SOURCE/'+name,'name':name,'size_bytes':12,'blake3':'a'*64} for name in names]}


class LayoutTests(unittest.TestCase):
    def test_dji_and_phones_are_behind_the_scenes(self):
        for device in ('DJI Osmo Pocket 3','大疆 Action 4','Apple iPhone 16 Pro','HUAWEI Pura 70',
                       'Xiaomi 14','vivo X200','OPPO Find X8','HONOR Magic 7','Samsung SM-S9280','Sony Xperia 1','Google Pixel 9'):
            with self.subTest(device=device):
                self.assertEqual(video_route(unit(device,['DCIM/clip.MP4']))[0],'2视频素材/侧拍')

    def test_camera_and_other_devices_stay_separate(self):
        for device in ('SONY ILCE-7RM5','Canon EOS R5','NIKON Z8','FUJIFILM X-H2'):
            self.assertEqual(video_route(unit(device,['clip.MOV']))[0],'2视频素材/相机')
        self.assertEqual(video_route(unit('Insta360 X4',['clip.MP4']))[0],'2视频素材/其他')

    def test_dji_card_evidence_routes_metadata_missing_videos(self):
        u=unit('设备未识别',['DCIM/DJI_001/DJI_20260813112732_0001_D.MP4','DCIM/DJI_001/DJI_20260813112732_0001_D.LRF'])
        self.assertEqual(video_route(u)[0],'2视频素材/侧拍')
        self.assertEqual(len(studio_target_rows(u,{'path':'2026/8月/示例'})),2)

    def test_unknown_and_conflicting_device_evidence_blocks(self):
        for u in (unit('设备未识别',['IMG_1234.MOV']),unit('设备未识别',['DJI_20260813112732_0001_D.MP4']),
                  unit('SONY ILCE-7RM5',['DCIM/DJI_001/DJI_20260813112732_0001_D.MP4']),
                  unit('DJI Pocket / SONY ILCE-7RM5',['clip.MP4'])):
            with self.assertRaises(ArchiveError): video_route(u)

    def test_camera_card_layout_is_evidence_but_filename_alone_is_not(self):
        self.assertEqual(video_route(unit('设备未识别',['M4ROOT/CLIP/C4860.MP4']))[0],'2视频素材/相机')
        with self.assertRaises(ArchiveError): video_route(unit('设备未识别',['C4860.MP4']))
        with self.assertRaises(ArchiveError): video_route(unit('DJI',['M4ROOT/CLIP/C4860.MP4']))

    def test_raw_jpg_and_sidecar_split_without_extra_wrapper_directories(self):
        rows=studio_target_rows(unit('SONY',['DCIM/A.ARW','DCIM/A.JPG','DCIM/A.XMP'],'photo'),{'path':'2026/7月/示例'})
        self.assertEqual([r['target_path'] for r in rows],['2026/7月/示例/1相机原素材/A.ARW',
                         '2026/7月/示例/4选片用JPG原片/A.JPG','2026/7月/示例/1相机原素材/A.XMP'])

    def test_mobile_photos_still_follow_photo_format(self):
        rows=studio_target_rows(unit('Apple iPhone 16',['IMG/A.JPG','IMG/B.DNG'],'photo'),{'path':'2026/7月/示例'})
        self.assertIn('/4选片用JPG原片/',rows[0]['target_path'])
        self.assertIn('/1相机原素材/',rows[1]['target_path'])

    def test_flattening_never_silently_overwrites_same_name(self):
        with self.assertRaises(ArchiveError):
            studio_target_rows(unit('SONY',['A/clip.MP4','B/CLIP.mp4']),{'path':'2026/7月/示例'})
