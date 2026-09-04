import unittest

import fixture_support


def extract_supervisor_block(doc_text: str) -> str:
    lines = doc_text.splitlines()
    start = lines.index("The supervisor MUST perform these steps; they are not delegated to workers.")
    end = lines.index("    afterward.", start)
    block = "\n".join(lines[start : end + 1]) + "\n"
    return block.replace("\r\n", "\n")


def extract_worker_block(doc_text: str) -> str:
    lines = doc_text.splitlines()
    anchor = None
    for index, line in enumerate(lines):
        if "The following block is included verbatim in every authoring worker prompt." in line:
            fence = lines.index("```text", index)
            closing = lines.index("```", fence + 1)
            block = "\n".join(lines[fence + 1 : closing]) + "\n"
            return block.replace("\r\n", "\n")
    raise AssertionError("worker directive anchor not found in the directives document")


class DirectiveFilesTest(unittest.TestCase):
    def test_worker_directive_matches_document_block_byte_for_byte(self):
        doc_text = fixture_support.DIRECTIVES_DOC.read_text(encoding="utf-8")
        expected = extract_worker_block(doc_text)
        actual = fixture_support.WORKER_DIRECTIVE_FILE.read_text(encoding="utf-8")
        self.assertEqual(actual.replace("\r\n", "\n"), expected)

    def test_supervisor_directive_matches_document_block_byte_for_byte(self):
        doc_text = fixture_support.DIRECTIVES_DOC.read_text(encoding="utf-8")
        expected = extract_supervisor_block(doc_text).replace("\r\n", "\n")
        actual = fixture_support.SUPERVISOR_DIRECTIVE_FILE.read_text(encoding="utf-8")
        self.assertEqual(actual.replace("\r\n", "\n"), expected)

    def test_directive_files_end_with_single_trailing_newline(self):
        for path in (fixture_support.WORKER_DIRECTIVE_FILE, fixture_support.SUPERVISOR_DIRECTIVE_FILE):
            raw = path.read_bytes()
            self.assertTrue(raw.endswith(b"\n"), path.name)
            self.assertFalse(raw.endswith(b"\n\n"), path.name)


if __name__ == "__main__":
    unittest.main()
