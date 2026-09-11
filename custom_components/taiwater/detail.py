"""依原站姓名彈窗與 EQueryForm 契約查詢明細；不保存原始頁面。"""
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .portal import TaiWaterClient, SiteSchemaChanged, QueryRejected, parse_document


class DetailParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.buttons = []
        self.months = []
        self.selected = None
        self.in_select = False
        self.option = None
        self.table_depth = 0
        self.cell = None
        self.cells = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == 'button':
            match = re.fullmatch(r"ShowInputName\('([0-9]{11})','(T[0-9]{5})'\);?", attrs.get('onclick', '').strip())
            if match:
                self.buttons.append(match.groups())
        if tag == 'select' and attrs.get('name') == 'TBName':
            self.in_select = True
        if tag == 'option' and self.in_select:
            self.option = [attrs.get('value', ''), [], 'selected' in attrs]
        if tag == 'table':
            if self.table_depth or attrs.get('id') == 'printTag':
                self.table_depth += 1
        if self.table_depth and tag in ('td', 'th'):
            self.cell = []

    def handle_data(self, text):
        if self.option is not None:
            self.option[1].append(text)
        if self.cell is not None:
            self.cell.append(text)

    def handle_endtag(self, tag):
        if tag == 'option' and self.option is not None:
            value, text, selected = self.option
            label = ''.join(text).strip()
            if re.fullmatch(r'T\d{5}', value) and re.fullmatch(r'\d{3}年\d{2}月', label):
                self.months.append({'value': value, 'label': label})
                if selected:
                    self.selected = value
            self.option = None
        if tag == 'select':
            self.in_select = False
        if tag in ('td', 'th') and self.cell is not None:
            self.cells.append(''.join(self.cell).strip())
            self.cell = None
        if tag == 'table' and self.table_depth:
            self.table_depth -= 1

    def fields(self):
        fields = {}
        private = ('名稱', '姓名', '地址', '水號', '表號', '編號', '查核碼', '電話', '證號')
        for cell in self.cells:
            parts = re.split('[：:]', cell, maxsplit=1)
            if len(parts) != 2:
                continue
            label, value = (re.sub(r'\s+', ' ', part).strip() for part in parts)
            if not label or len(label) > 35 or any(word in label for word in private):
                continue
            fields[label] = value
        if '應繳總金額' not in fields or '用水度數' not in fields:
            raise QueryRejected('明細尚未通過姓名驗證，或明細格式已變更。')
        return fields


def detail_form(html, url):
    forms = [f for f in parse_document(html).forms
             if urlsplit(urljoin(url, f.action)).path == '/ch/EQuery/WaterFeeQueryDetail'
             and f.method == 'post']
    if len(forms) != 1:
        raise SiteSchemaChanged('找不到唯一的台水姓名／月份明細表單。')
    form = forms[0]
    names = {c.name for c in form.controls}
    selection = DetailParser()
    selection.feed(html)
    if selection.months:
        names.add('TBName')
    if not {'Name', 'WaterNo', 'TBName', '__RequestVerificationToken'} <= names:
        raise SiteSchemaChanged('台水明細表單缺少必要欄位。')
    target = urljoin(url, form.action)
    if urlsplit(target).netloc != 'www.water.gov.tw' or urlsplit(target).scheme != 'https':
        raise SiteSchemaChanged('台水明細目的地不符預期。')
    return form, target


class DetailClient(TaiWaterClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.detail_state = None
        self.months = []
        self.summary_months = []

    def _request(self, url, **kwargs):
        result = super()._request(url, **kwargs)
        body, kind, final_url = result
        if urlsplit(final_url).path == '/ch/EQuery/WaterFeeQuerySearch' and kind == 'text/html':
            html = self._decode(body)
            parser = DetailParser()
            parser.feed(html)
            if parser.buttons:
                form, target = detail_form(html, final_url)
                self.detail_state = (form.hidden_payload(), target, final_url)
                self.summary_months = parser.buttons
        return result

    def open_details(self, name, water_id):
        name = name.strip()
        if not name or len(name) > 100:
            raise ValueError('請輸入姓名。')
        if not self.detail_state:
            raise SiteSchemaChanged('查詢結果沒有可辨識的姓名驗證表單。')
        months = [month for water, month in self.summary_months if water == water_id]
        if not months:
            raise SiteSchemaChanged('明細按鈕與本次查詢水號不一致。')
        payload, target, referer = self.detail_state
        return self._fetch_details({**payload, 'Name': name, 'WaterNo': water_id, 'TBName': months[0]}, target, referer)

    def select_month(self, month):
        if not self.detail_state or month not in {m['value'] for m in self.months}:
            raise ValueError('請選擇台水提供的帳期。')
        payload, target, referer = self.detail_state
        return self._fetch_details({**payload, 'TBName': month}, target, referer)

    def _fetch_details(self, payload, target, referer):
        body, kind, url = self._request(target, data=payload, referer=referer)
        if kind != 'text/html':
            raise SiteSchemaChanged('台水明細未回傳 HTML。')
        html = self._decode(body)
        parser = DetailParser()
        parser.feed(html)
        fields = parser.fields()
        form, target = detail_form(html, url)
        selected = payload['TBName']
        if selected not in {m['value'] for m in parser.months}:
            raise SiteSchemaChanged('台水未回傳要求的帳期。')
        # 驗證可見帳期，不能將登入錯誤或另一月份當成本次成功。
        headings = [re.fullmatch(r'(\d{3})年(\d{1,2})月份', re.sub(r'\s+', '', c)) for c in parser.cells]
        if not any(m and int(m[1]) == int(selected[1:4]) and int(m[2]) == int(selected[4:]) for m in headings):
            raise SiteSchemaChanged('明細顯示帳期與選擇月份不一致。')
        self.detail_state = (form.hidden_payload(), target, url)
        self.months = parser.months
        return {'fields': fields, 'months': self.months, 'selected_month': selected}
