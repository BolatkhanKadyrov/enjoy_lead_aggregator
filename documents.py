"""Read the POS export and prepare printable kitchen and delivery documents."""
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from io import BytesIO
from pathlib import Path
import re
from xml.sax.saxutils import escape
from zipfile import BadZipFile, ZipFile

from openpyxl import load_workbook
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Paragraph


HEADERS = {
    'id': '#', 'point': 'клиент', 'gross': 'сумма чека', 'paid': 'оплачено',
    'discount': 'скидка в чеке', 'name': 'название товаров', 'price': 'цена товаров',
    'qty': 'кол-во товаров', 'line_discount': 'скидки на товары', 'total': 'итоговая цена товаров',
}
SERVICE_PREFIX = re.compile(r'^[a-zа-яё]\)\s*', re.I)
EXCHANGE = re.compile(r'(?:\s*\(\s*обмен\s*\)|\s*\[\s*обмен\s*\]|(?:^|\s+)обмен)\s*$', re.I)
MAX_BYTES = 10 * 1024 ** 2
MAX_UNPACKED = 48 * 1024 ** 2
MAX_ROWS = 12000
MAX_COLS = 128


def _text(value):
    return ' '.join(str(value if value is not None else '').split())


def _name(value, field):
    name = SERVICE_PREFIX.sub('', _text(value)).strip()
    if not name or len(name) > 250:
        raise ValueError(f'пустое или слишком длинное поле «{field}»')
    return name


def _number(value, field):
    raw = _text(value).replace(' ', '').replace(',', '.')
    if len(raw) > 32:
        raise ValueError(f'слишком большое значение «{field}»')
    try:
        number = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f'неверное значение «{field}»') from None
    if not number.is_finite() or number < 0 or number > 10**13 or number.as_tuple().exponent < -8:
        raise ValueError(f'недопустимое число в поле «{field}»')
    return number


def _money(value, field):
    return format(_number(value, field).normalize(), 'f')


def _receipt_id(value):
    raw = _text(value)
    # A POS number may arrive as a numeric cell or zero-padded text.
    if isinstance(value, str) and re.fullmatch(r'[0-9]{1,24}', raw):
        return raw
    if len(raw) > 24 or not re.fullmatch(r'[0-9]+(?:\.0+)?', raw):
        raise ValueError('некорректный номер чека')
    return raw.partition('.')[0]


def _read_rows(sheet):
    # Some exporters omit or misstate worksheet dimensions.
    sheet.reset_dimensions()
    headers = None
    receipts = {}
    for row_number, row in enumerate(sheet.iter_rows(max_col=MAX_COLS, values_only=True), 1):
        if row_number > MAX_ROWS:
            raise ValueError(f'Поддерживается до {MAX_ROWS} строк на лист.')
        if not any(v is not None and v != '' for v in row):
            continue
        if headers is None:
            labels = [_text(v).casefold() for v in row]
            if '#' not in labels:
                if row_number >= 25:
                    break
                continue
            missing = [label for label in HEADERS.values() if labels.count(label) != 1]
            if missing:
                raise ValueError('Проверьте названия и повторы столбцов: ' + ', '.join(missing))
            headers = {key: labels.index(label) for key, label in HEADERS.items()}
            continue
        values = {key: row[index] for key, index in headers.items()}
        try:
            receipt_id = _receipt_id(values['id'])
            point = _name(values['point'], 'Клиент')
            summary = {key: _money(values[key], HEADERS[key]) for key in ('paid', 'discount', 'gross')}
            receipt = receipts.setdefault(receipt_id, dict(id=receipt_id, point=point, lines=[], **summary))
            if point.casefold() != receipt['point'].casefold() or any(receipt[k] != v for k, v in summary.items()):
                raise ValueError(f'чек № {receipt_id} имеет разные реквизиты')
            raw_name = _name(values['name'], 'Название товаров')
            exchange = bool(EXCHANGE.search(raw_name))
            name = _name(EXCHANGE.sub('', raw_name), 'Название товаров')
            qty = _number(values['qty'], HEADERS['qty'])
            if qty < 1 or qty != qty.to_integral_value() or qty > 1000000:
                raise ValueError('количество должно быть положительным целым числом до 1 000 000')
            line = dict(name=name, exchange=exchange, qty=int(qty), price=_money(values['price'], HEADERS['price']),
                        discount=_money(values['line_discount'], HEADERS['line_discount']),
                        total=_money(values['total'], HEADERS['total']))
            if exchange and Decimal(line['total']):
                raise ValueError(f'платная строка «ОБМЕН» в чеке № {receipt_id}')
            receipt['lines'].append(line)
        except ValueError as exc:
            raise ValueError(f'Строка {row_number}: {exc}.') from None
    if headers is None:
        raise ValueError('Заголовок выгрузки чеков не найден.')
    if not receipts:
        raise ValueError('В выгрузке нет товарных строк.')
    return list(receipts.values())


def parse_workbook(data: bytes) -> list[dict]:
    if len(data) > MAX_BYTES:
        raise ValueError('Файл превышает 10 МБ.')
    try:
        with ZipFile(BytesIO(data)) as zf:
            files = zf.infolist()
            if len(files) > 800 or sum(f.file_size for f in files) > MAX_UNPACKED:
                raise ValueError('Содержимое XLSX слишком велико.')
            if any(f.flag_bits & 1 for f in files):
                raise ValueError('Файл XLSX защищён паролем.')
    except BadZipFile:
        raise ValueError('Файл должен быть книгой Excel в формате XLSX.') from None
    try:
        workbook = load_workbook(BytesIO(data), read_only=True, data_only=True, keep_links=False)
    except Exception as exc:
        raise ValueError('Не удалось прочитать книгу Excel.') from exc
    try:
        matches = [s for s in workbook.worksheets if s.title.strip().casefold() == 'чеки']
        if matches:
            sheet = matches[0]
        elif len(workbook.worksheets) == 1:
            sheet = workbook.worksheets[0]
        else:
            raise ValueError('В книге с несколькими листами нужен лист «Чеки».')
        return _read_rows(sheet)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError('Повреждён лист Excel с чеками.') from exc
    finally:
        workbook.close()


W, H = A4
M = 31
WIDTH = W - 2 * M
INK = colors.CMYKColor(0, 0, 0, 1)
MUTED = colors.CMYKColor(0, 0, 0, .80)
RULE = colors.CMYKColor(0, 0, 0, .52)


@lru_cache(maxsize=1)
def _load_fonts():
    roots = (('/usr/share/fonts/truetype/dejavu', 'DejaVuSans.ttf', 'DejaVuSans-Bold.ttf'),
             ('C:/Windows/Fonts', 'arial.ttf', 'arialbd.ttf'),
             ('/System/Library/Fonts/Supplemental', 'Arial.ttf', 'Arial Bold.ttf'))
    for directory, normal, bold in roots:
        if all((Path(directory) / f).is_file() for f in (normal, bold)):
            for key, file in (('Kitchen', normal), ('KitchenBold', bold)):
                pdfmetrics.registerFont(TTFont(key, str(Path(directory) / file)))
            return
    raise RuntimeError('Для PDF требуется шрифт DejaVu Sans.')


def _amount(n):
    value = Decimal(n)
    s = format(value, ',f')
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s.replace(',', ' ').replace('.', ',')


def _para(s, size=9, bold=False, right=False, color=INK):
    style = ParagraphStyle('cell', fontName='KitchenBold' if bold else 'Kitchen', fontSize=size,
                           leading=size * 1.25, alignment=TA_RIGHT if right else TA_LEFT, textColor=color)
    return Paragraph(escape(str(s)), style)


def _cell(pdf, p, x, top, width):
    _, h = p.wrap(width, H)
    if pdf is not None:
        p.drawOn(pdf, x, top - h)
    return top - h


def _rule(pdf, x1, y, x2, dashed=False, *, weight=0.55, color=RULE):
    pdf.saveState()
    pdf.setStrokeColor(color)
    pdf.setLineWidth(weight)
    if dashed:
        pdf.setDash(4, 3)
    pdf.line(x1, y, x2, y)
    pdf.restoreState()


def _sheet_footer(pdf, caption):
    _rule(pdf, M, 25, W-M)
    _cell(pdf, _para(caption, 7, color=MUTED), M, 21, WIDTH-55)
    _cell(pdf, _para(str(pdf.getPageNumber()), 7, right=True, color=MUTED), W-M-42, 21, 42)


def _dates(collected, delivery):
    return f'Сбор заявок {collected:%d.%m.%Y}    /    Доставка {delivery:%d.%m.%Y}'


def _production_pdf(receipts, catalog, collected, delivery):
    out = BytesIO()
    pdf = Canvas(out, pagesize=A4)
    pdf.setTitle('План производства и отчёт по выгрузке')
    counts = Counter()
    names = {}
    for r in receipts:
        for line in r['lines']:
            key = line['name'].casefold()
            names.setdefault(key, line['name'])
            counts[key] += line['qty']
    groups = defaultdict(list)
    for key, count in counts.items():
        section = catalog['dishes'].get(key)
        if not section:
            raise ValueError(f'Не выбран раздел блюда «{names[key]}».')
        groups[section].append((names[key], count))

    def plan_heading():
        _cell(pdf, _para('План производства', 24, True), M, H-M, WIDTH)
        _cell(pdf, _para(f'Готовим на {delivery:%d.%m.%Y}', 17, True), M, H-M-38, WIDTH*.57)
        _cell(pdf, _para(f'Заявки собраны {collected:%d.%m.%Y}', 11), M, H-M-63, WIDTH*.57)
        summary_left = M + WIDTH*.58
        _cell(pdf, _para(f'Всего порций {sum(counts.values())}', 15, True, True),
              summary_left, H-M-40, WIDTH*.42)
        _cell(pdf, _para(f'Позиций {len(counts)}', 11, right=True), summary_left, H-M-63, WIDTH*.42)

    plan_heading()
    gutter = 22
    cw = (WIDTH-gutter)/2
    top = H - M - 94
    col = 0
    y = top

    def next_column():
        nonlocal col, y
        if col == 1:
            _sheet_footer(pdf, 'Кухня')
            pdf.showPage()
            plan_heading()
        col = 1-col
        y = top

    def group_heading(section, total, continued=False):
        x = M+col*(cw+gutter)
        title = section + (' (прод.)' if continued else '')
        p = _para(title, 13, True)
        units = _para(f'{total} шт.', 11, True, True)
        h = max(p.wrap(cw-70, H)[1], units.wrap(65, H)[1]) + 10
        _rule(pdf, x, y, x+cw, weight=1.3, color=INK)
        _cell(pdf, p, x, y-5, cw-70)
        _cell(pdf, units, x+cw-65, y-6, 65)
        _rule(pdf, x, y-h, x+cw)
        return y-h

    entries = []
    qty_width = 40
    width = cw-qty_width-8
    for section in dict.fromkeys([*catalog['sections'], *groups]):
        items = sorted(groups.get(section, ()), key=lambda v: (-v[1], v[0].casefold()))
        if not items:
            continue
        total = sum(q for _, q in items)
        paragraphs = []
        for name, qty in items:
            label = _para(name, 11.5)
            number = _para(qty, 12.5, True, True)
            row_h = max(label.wrap(width,H)[1], number.wrap(qty_width,H)[1]) + 7
            paragraphs.append((label, number, row_h))
        heading_h = max(_para(section, 13, True).wrap(cw-70,H)[1],
                        _para(f'{total} шт.', 11, True).wrap(65,H)[1]) + 10
        height = heading_h + sum(row_h for _, _, row_h in paragraphs) + 13
        entries.append((section, total, paragraphs, heading_h, height))
    # When the plan fits on one sheet, balance the columns at a section heading.
    total_height = sum(entry[4] for entry in entries)
    heights = 0
    candidates = []
    for pos, entry in enumerate(entries[:-1], 1):
        heights += entry[4]
        if max(heights, total_height-heights) <= top-39:
            candidates.append((abs(2*heights-total_height), pos))
    split_at = min(candidates)[1] if candidates else None
    for pos, (section, total, paragraphs, heading_h, _) in enumerate(entries):
        first_h = paragraphs[0][2]
        if pos == split_at:
            next_column()
        if y-heading_h-first_h < 39:
            next_column()
        y = group_heading(section, total)
        for label, number, row_h in paragraphs:
            if y-row_h < 39:
                next_column()
                y = group_heading(section, total, True)
            x = M+col*(cw+gutter)
            _cell(pdf, label, x, y-3, width)
            _cell(pdf, number, x+cw-qty_width, y-2, qty_width)
            y -= row_h
            _rule(pdf, x, y, x+cw)
        y -= 13
    _sheet_footer(pdf, 'Кухня')
    pdf.showPage()

    def report_heading():
        _cell(pdf, _para('Отчёт по выгрузке', 22, True), M, H-M, WIDTH)
        _cell(pdf, _para(_dates(collected, delivery), 10, color=MUTED), M, H-M-33, WIDTH)
        return H-M-59

    y = report_heading()

    def new_report_page():
        nonlocal y
        _sheet_footer(pdf, 'Контроль выгрузки')
        pdf.showPage()
        y = report_heading()

    def section_title(label, room=24):
        nonlocal y
        if y - 34 - room < 39:
            new_report_page()
        y -= 9
        _rule(pdf, M, y, W-M)
        y = _cell(pdf, _para(label, 13, True), M, y-6, WIDTH)-4

    included, excluded = [], []
    for r in receipts:
        info = catalog['points'].get(r['point'].casefold(), {})
        (excluded if info.get('excluded') else included).append(r)
    half = WIDTH/2
    for left, right in [
        (f'Чеков в Excel  {len(receipts)}', f'Накладных к печати  {len(included)}'),
        (f'Порций в плане  {sum(counts.values())}', f'Разных блюд  {len(counts)}')
    ]:
        bottom_left = _cell(pdf, _para(left, 12, True), M, y, half-8)
        bottom_right = _cell(pdf, _para(right, 12, True), M+half, y, half)
        y = min(bottom_left, bottom_right)-6

    section_title('Исключены из накладных')
    for point in sorted({r['point'] for r in excluded}, key=str.casefold) or ['Нет']:
        p = _para(point, 11)
        if y - p.wrap(WIDTH, H)[1] < 39:
            new_report_page()
            section_title('Исключены из накладных (продолжение)')
        y = _cell(pdf, p, M, y, WIDTH)-5

    section_title('Итоги по напечатанным накладным', room=66)
    for label, field in [('Сумма товаров', 'gross'), ('Скидка по чекам', 'discount'), ('К оплате', 'paid')]:
        total = _amount(sum(Decimal(r[field]) for r in included))
        bottom_label = _cell(pdf, _para(label, 11, field == 'paid'), M, y, half)
        bottom_total = _cell(pdf, _para(total + ' ₸', 11, field == 'paid', True), M+half, y, half)
        y = min(bottom_label, bottom_total)-5

    section_title('Расхождения по чекам', room=83)
    y = _cell(pdf, _para('Разница = «Оплачено» минус сумма итоговых цен товарных строк.', 10),
              M, y, WIDTH)-8
    widths = (WIDTH-239, 83, 83, 73)

    def discrepancy_row(values, heading=False, measure=False):
        cells = [_para(text, 9 if heading else 10, heading or i == 3, i > 0)
                 for i, text in enumerate(values)]
        height = max(p.wrap(w-8, H)[1] for p, w in zip(cells, widths))+9
        if not measure:
            x = M
            for p, w in zip(cells, widths):
                _cell(pdf, p, x+4, y-4, w-8)
                x += w
            _rule(pdf, M, y-height, W-M)
        return height

    labels = ('Чек и точка', 'По строкам, ₸', 'Оплачено, ₸', 'Разница, ₸')
    show_labels = True
    for r in receipts:
        lines = sum(Decimal(line['total']) for line in r['lines'])
        paid = Decimal(r['paid'])
        if lines == paid:
            continue
        row = (f'№ {r["id"]}  ·  {r["point"]}', _amount(lines), _amount(paid), _amount(paid-lines))
        row_height = discrepancy_row(row, measure=True)
        required = row_height + (discrepancy_row(labels, heading=True, measure=True) if show_labels else 0)
        if y-required < 39:
            new_report_page()
            section_title('Расхождения по чекам (продолжение)')
            show_labels = True
        if show_labels:
            y -= discrepancy_row(labels, heading=True)
            show_labels = False
        y -= discrepancy_row(row)
    if show_labels:
        _cell(pdf, _para('Расхождений нет.', 11), M, y, WIDTH)
    _sheet_footer(pdf, 'Контроль выгрузки')
    pdf.save()
    return out.getvalue()

INVOICE_WIDTHS = (23, WIDTH - 23 - 43 - 57 - 58 - 72, 43, 57, 58, 72)


def _table_row(pdf, cells, x, top, heading=False):
    sizes = (8.5,) * 6 if heading else (8.5, 10, 10.5, 9.5, 9.5, 9.5)
    paragraphs = [_para(value, sizes[i], bold=heading or i == 2, right=i >= 2)
                  for i, value in enumerate(cells)]
    height = max(p.wrap(w - 6, H)[1] for p, w in zip(paragraphs, INVOICE_WIDTHS)) + 4
    if pdf is not None:
        if heading:
            _rule(pdf, M, top, W - M)
        for paragraph, width in zip(paragraphs, INVOICE_WIDTHS):
            _cell(pdf, paragraph, x + 3, top - 2, width - 6)
            x += width
        _rule(pdf, M, top - height, W - M)
    return height


def _invoice_header(pdf, top, r, info, collected, delivery, part=0):
    title = f'Накладная № {r["id"]}' + (f'  ·  Продолжение {part}' if part else '')
    date_width = 142
    left_width = WIDTH - date_width - 12
    date_x = W - M - date_width
    title_end = _cell(pdf, _para(title, 12, True), M, top, left_width)
    date_end = _cell(pdf, _para(f'Доставка {delivery:%d.%m.%Y}', 11, True, True),
                     date_x, top, date_width)
    y = min(title_end, date_end) - 4
    point_end = _cell(pdf, _para(info.get('name', r['point']), 15, True), M, y, left_width)
    collected_end = _cell(pdf, _para(f'Сбор заявок {collected:%d.%m.%Y}', 9, right=True),
                          date_x, y, date_width)
    y = min(point_end, collected_end) - 2
    address = info.get('address') or ''
    y = _cell(pdf, _para('Адрес: ' + address, 10), M, y, WIDTH)
    if pdf is not None and not address:
        _rule(pdf, M + 35, y + 1, M + 240)
    y -= 6
    headings = ('№', 'Наименование', 'Кол-во', 'Цена', 'Скидка', 'Итого')
    return y - _table_row(pdf, headings, M, y, True)


def _invoice_end(pdf, top, r):
    qty = sum(line['qty'] for line in r['lines'])
    qty_end = _cell(pdf, _para(f'Всего, шт.: {qty}', 10, True), M, top - 5, 135)
    totals = f'Сумма товаров: {_amount(r["gross"])} ₸     Скидка: {_amount(r["discount"])} ₸'
    totals_end = _cell(pdf, _para(totals, 9.5, right=True), M + 145, top - 5, WIDTH - 145)
    y = min(qty_end, totals_end) - 7
    paid = f'Итого к оплате: {_amount(r["paid"])} ₸'
    paid_end = _cell(pdf, _para(paid, 13, True, True), M + 225, y, WIDTH - 225)
    courier_end = _cell(pdf, _para('Курьер:', 10), M, y - 6, 45)
    if pdf is not None:
        _rule(pdf, M + 45, courier_end + 1, M + 211)
    return min(courier_end, paid_end)


def _invoices_pdf(receipts, catalog, collected, delivery):
    out = BytesIO()
    pdf = Canvas(out, pagesize=A4)
    pdf.setTitle('Накладные по точкам')
    half_height = H/2-M-9
    pending_half = False
    printed = False
    for r in sorted(receipts, key=lambda r: (int(r['id']),r['id'])):
        info = catalog['points'].get(r['point'].casefold(), {})
        if info.get('excluded'):
            continue
        printed = True
        rows = []
        for i, line in enumerate(r['lines'], 1):
            name = line['name'] + (' (ОБМЕН)' if line['exchange'] else '')
            values = (i, name, line['qty'], _amount(line['price']), _amount(line['discount']), _amount(line['total']))
            rows.append((values,_table_row(None,values,M,0)))
        head_h = -_invoice_header(None, 0, r, info, collected, delivery)
        tail_h = -_invoice_end(None,0,r)
        height = head_h+sum(h for _,h in rows)+tail_h
        if height <= half_height + 0.001:
            if pending_half:
                _rule(pdf, M, H/2, W-M, True)
            y = _invoice_header(pdf, H/2-10 if pending_half else H-M, r, info, collected, delivery)
            for values, h in rows:
                _table_row(pdf,values,M,y)
                y -= h
            _invoice_end(pdf,y,r)
            if pending_half:
                pdf.showPage()
            pending_half = not pending_half
            continue
        if pending_half:
            pdf.showPage()
            pending_half = False
        part = 0
        y = _invoice_header(pdf, H-M, r, info, collected, delivery)
        for i,(values,h) in enumerate(rows):
            reserved = tail_h if i == len(rows)-1 else 14
            if y-h-reserved < M:
                _cell(pdf,_para(f'Продолжение накладной № {r["id"]} на следующем листе', 8, color=MUTED),M,M+8,WIDTH)
                pdf.showPage()
                part += 1
                y = _invoice_header(pdf, H-M, r, info, collected, delivery, part)
            _table_row(pdf,values,M,y)
            y -= h
        _invoice_end(pdf,y,r)
        pdf.showPage()
    if not printed:
        y = _cell(pdf,_para('Накладные по точкам', 20, True), M,H-M,WIDTH)
        y = _cell(pdf,_para(_dates(collected, delivery),10),M,y-9,WIDTH)
        _cell(pdf,_para('Все точки в этой выгрузке исключены из накладных.', 11),M,y-20,WIDTH)
    pdf.save()
    return out.getvalue()


def render_documents(receipts: list[dict], catalog: dict, collected: date, delivery: date) -> tuple[bytes,bytes]:
    _load_fonts()
    return (_production_pdf(receipts, catalog, collected, delivery),
            _invoices_pdf(receipts, catalog, collected, delivery))
