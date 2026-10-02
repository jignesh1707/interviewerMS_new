"""Child-process entry point for parsing untrusted documents.

Usage: python -m app.services._doc_worker <pdf|docx> <max_pages> < file-bytes > text
Running the parser in a separate process lets the parent enforce a hard timeout and keeps
parser crashes or memory blowups away from the API process.
"""

import io
import sys


def main() -> int:
    kind = sys.argv[1]
    max_pages = int(sys.argv[2])
    content = sys.stdin.buffer.read()
    if kind == "pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        if len(reader.pages) > max_pages:
            sys.stderr.write(f"too_many_pages:{len(reader.pages)}")
            return 3
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    elif kind == "docx":
        from docx import Document

        document = Document(io.BytesIO(content))
        text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    else:
        return 2
    sys.stdout.buffer.write(text.encode("utf-8", errors="ignore"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
