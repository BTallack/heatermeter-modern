"""Tests for note photo storage + the path-traversal guard."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd.store import Store


def test_note_photo_storage_and_listing():
    d = tempfile.mkdtemp()
    st = Store(os.path.join(d, "hm.sqlite"))
    name = st.save_photo(b"\xff\xd8\xff\xe0fakejpegbytes", "jpg")
    assert name and name.endswith(".jpg")
    assert os.path.exists(os.path.join(st.photos_dir, name))
    nid = st.add_note(123.0, "wrapped the brisket", photo=name)
    assert nid > 0
    rows = st.list_notes()
    assert rows[-1]["photo"] == name
    assert rows[-1]["text"] == "wrapped the brisket"


def test_photo_fullpath_guards_traversal():
    d = tempfile.mkdtemp()
    st = Store(os.path.join(d, "hm.sqlite"))
    name = st.save_photo(b"img", "png")
    assert st.photo_fullpath(name) is not None          # real file resolves
    assert st.photo_fullpath("../hm.sqlite") is None     # traversal blocked
    assert st.photo_fullpath("../../etc/passwd") is None  # traversal blocked
    assert st.photo_fullpath("sub/evil.png") is None      # no subpaths
    assert st.photo_fullpath("nope.png") is None          # nonexistent


def test_inmemory_store_has_no_photo_storage():
    assert Store(":memory:").save_photo(b"x", "jpg") is None
    assert Store(":memory:").photo_fullpath("x.jpg") is None
