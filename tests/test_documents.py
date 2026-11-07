import unittest
from datetime import date
from io import BytesIO
from zipfile import ZipFile

from openpyxl import Workbook
from pypdf import PdfReader

from documents import HEADERS, parse_workbook, render_documents


def export(rows):
    book = Workbook()
    sheet = book.active
    sheet.title = 'Чеки'
    sheet.append([None])
    sheet.append(list(HEADERS.values()))
    for row in rows:
        sheet.append(row)
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def row(number, point, name, qty, price, total, paid, gross, discount=0):
    # Same POS columns as HEADERS, deliberately using a different order from the real export.
    return [number, point, gross, paid, discount, name, price, qty, 0, total]


def pages(data):
    return [page.extract_text() for page in PdfReader(BytesIO(data)).pages]


class DocumentsTest(unittest.TestCase):
    def setUp(self):
        self.catalog = {
            'sections': ['Завтраки'], 'dishes': {'сыр <морс> & чай': 'Завтраки'},
            'points': {'точка & зал': {'name': 'Точка & Зал', 'address': 'Улица, 2', 'excluded': False},
                       'сеть': {'name': 'Сеть', 'address': '', 'excluded': True}},
        }
        self.dish = 'Сыр <морс> & чай'
        self.rows = [
            row('009', 'a) Точка & Зал', 'b) '+self.dish, 1, 100, '99,5', 99, 100, '0,5'),
            row('009', 'a) Точка & Зал', self.dish+' (ОБМЕН)', 2, 0, 0, 99, 100, '0,5'),
            row(10, 'Точка & Зал', self.dish, 3, 100, 300, 300, 300),
            row(11, 'Сеть', self.dish, 4, 100, 400, 400, 400),
        ]

    def render(self, rows):
        receipts = parse_workbook(export(rows))
        return render_documents(receipts, self.catalog, date(2026,10,7), date(2026,10,8))

    def test_separate_receipts_and_exchange(self):
        records = parse_workbook(export(self.rows))
        self.assertEqual([r['id'] for r in records], ['009','10','11'])
        self.assertEqual(records[0]['point'], 'Точка & Зал')
        self.assertEqual(records[0]['lines'][0]['name'], self.dish)
        self.assertEqual(records[0]['lines'][1]['name'], self.dish)
        self.assertTrue(records[0]['lines'][1]['exchange'])
        self.assertEqual(records[0]['paid'], '99')
        self.assertEqual(records[0]['lines'][0]['total'], '99.5')
        plan, invoices = self.render(self.rows)
        production, report = pages(plan)
        self.assertIn('Готовим на 08.10.2026', production)
        self.assertIn('Заявки собраны 07.10.2026', production)
        self.assertIn('Всего порций 10', production)
        self.assertIn('Позиций 1', production)
        self.assertIn('Накладных к печати 2', report)
        self.assertIn('разница -0,5 ₸', report)
        printed = pages(invoices)
        self.assertEqual(len(printed), 1)
        self.assertIn('Доставка 08.10.2026', printed[0])
        for token in ['Накладная № 009','Накладная № 10','(ОБМЕН)','Итого к оплате: 99 ₸', self.dish, 'Курьер:']:
            self.assertIn(token, printed[0])
        self.assertNotIn('Накладная № 11', printed[0])

    def test_long_invoice_retains_lines_and_single_total(self):
        lines = [row(1,'Точка & Зал',self.dish,1,100,100,15000,15000) for _ in range(150)]
        _, invoices = self.render(lines)
        printed = pages(invoices)
        self.assertGreater(len(printed), 1)
        text = '\n'.join(printed)
        self.assertEqual(text.count(self.dish), 150)
        self.assertEqual(text.count('Итого к оплате:'), 1)
        self.assertEqual(text.count('Накладная № 1'), len(printed))

    def test_full_discount_exchange_with_standalone_suffix(self):
        for suffix in [' оБмеН', ' [ОБМЕН]', ' (ОБМЕН)']:
            values = row(22, 'Сеть', self.dish+suffix, 1, 100, 0, 0, 100, 100)
            values[-2] = 100
            line = parse_workbook(export([values]))[0]['lines'][0]
            self.assertEqual((line['name'], line['total'], line['exchange']), (self.dish, '0', True))

    def test_all_excluded_still_returns_printable_documents(self):
        plan, invoices = self.render(self.rows[-1:])
        self.assertIn('Порций в плане 4', pages(plan)[-1])
        self.assertIn('Все точки', pages(invoices)[0])

    def test_rejects_bad_rows_and_conflicting_summary(self):
        for changed, pattern in [
            (row('009','Другая',self.dish,1,100,100,99,100),'разные реквизиты'),
            (row('2','Точка',self.dish,0.5,100,50,50,50),'положительным целым'),
            (row('2','Точка',self.dish+' (ОБМЕН)',1,100,100,100,100),'платная строка'),
        ]:
            with self.subTest(pattern=pattern), self.assertRaisesRegex(ValueError, pattern):
                parse_workbook(export([self.rows[0],changed]))

    def test_rejects_non_workbook_and_excess_archive_members(self):
        with self.assertRaisesRegex(ValueError,'XLSX'):
            parse_workbook(b'broken')
        buf = BytesIO()
        with ZipFile(buf,'w') as archive:
            for i in range(801):
                archive.writestr(str(i),'')
        with self.assertRaisesRegex(ValueError,'слишком велико'):
            parse_workbook(buf.getvalue())


if __name__ == '__main__':
    unittest.main()
