import tempfile
import unittest
from pathlib import Path

import check_tokens as ct


def rules(text: str) -> list[tuple[str, str]]:
    return [(f.rule, f.match) for f in ct.scan_text(text, Path("x.vue"))]


class ScanTextTest(unittest.TestCase):
    def test_arbitrary_values_flagged(self):
        self.assertEqual(rules('<p class="text-[15px] w-[300px]">'),
                         [("arbitrary", "text-[15px]"), ("arbitrary", "w-[300px]")])

    def test_important_arbitrary_flagged(self):
        self.assertEqual(rules('class="!h-[40px]"'), [("arbitrary", "!h-[40px]")])

    def test_palette_colours_flagged(self):
        self.assertEqual(rules('class="bg-emerald-600 text-amber-500/80 text-white"'),
                         [("palette", "bg-emerald-600"), ("palette", "text-amber-500/80"), ("palette", "text-white")])

    def test_numeric_spacing_flagged(self):
        self.assertEqual(rules('class="px-6 gap-1.5 size-4 -mt-1"'),
                         [("spacing", "px-6"), ("spacing", "gap-1.5"), ("spacing", "size-4"), ("spacing", "-mt-1")])

    def test_stock_type_scale_flagged(self):
        self.assertEqual(rules('class="text-sm text-2xl"'), [("type", "text-sm"), ("type", "text-2xl")])

    def test_inline_style_flagged(self):
        self.assertEqual([r for r, _ in rules('<div style="x" :style="y">')], ["inline", "inline"])
        self.assertEqual([r for r, _ in rules("<style scoped>")], ["inline"])

    def test_tokens_and_keywords_pass(self):
        ok = ('class="px-cell-x gap-toolbar size-icon text-row-title text-body bg-allow text-tone-allow '
              'w-full h-auto p-0 -translate-y-1/2 top-1/2 row-meta pill pill-warn text-muted-foreground"')
        self.assertEqual(rules(ok), [])

    def test_reports_line_numbers(self):
        f = ct.scan_text("ok\nclass=\"px-6\"\n", Path("a.vue"))
        self.assertEqual((f[0].line, str(f[0])), (2, "a.vue:2: spacing: px-6"))


class ScanTreeTest(unittest.TestCase):
    def test_generated_ui_and_lib_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d)
            (src / "components/ui/button").mkdir(parents=True)
            (src / "lib").mkdir()
            (src / "components/ui/button/Button.vue").write_text('class="px-6"')
            (src / "lib/utils.ts").write_text('"text-sm"')
            (src / "components/Row.vue").write_text('class="px-6"')
            (src / "notes.md").write_text("px-6")
            findings, files = ct.scan(src)
            self.assertEqual(files, 1)
            self.assertEqual([f.path.name for f in findings], ["Row.vue"])

    def test_main_exit_codes(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "A.vue").write_text('class="p-card"')
            self.assertEqual(ct.main([d]), 0)
            (Path(d) / "B.vue").write_text('class="p-4"')
            self.assertEqual(ct.main([d]), 1)


if __name__ == "__main__":
    unittest.main()
