"""验证真实下载、用途拒绝、文件上传与独立受理状态。"""
from support import *
class Portal(SourceCase):
    def test_download_upload_and_review_contract(self):
        page=self.browser();page.goto(self.base+'/traveler/P-201')
        with page.expect_download() as item: page.get_by_role('link',name='下载 passport PP201-v1').click()
        download=item.value;self.assertEqual(download.suggested_filename,'passport.txt');self.assertIn('FAKE-000201',Path(download.path()).read_text(encoding='utf-8'))
        page.goto(self.base+'/traveler/P-202');page.get_by_role('link',name='下载 passport PP202-v1').click();self.assertIn('delegation_expired',page.locator('body').inner_text())
        page.goto(self.base+'/intake');page.locator('[name=reference]').fill('source-check');page.locator('[name=metadata]').fill(json.dumps({'traveler':'P-201','trip':'T1','purpose':'tour','documents':{'passport':'PP201-v1'}}));page.locator('[name=passport]').set_input_files({'name':'passport.txt','mimeType':'text/plain','buffer':b'isolated transport probe'});page.get_by_role('button',name='送审').click();page.get_by_role('link',name='intake-1').click();self.assertIn('results',page.locator('body').inner_text())
        self.control(1);page.reload();self.assertIn('needs_replacement',page.locator('body').inner_text());self.screenshot(page)
        self.assertEqual(self.request('/download/PP201-v2?traveler=P-201&purpose=other')[0],403)
