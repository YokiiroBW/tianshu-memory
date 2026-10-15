from contextlib import contextmanager


def test_keyword_index_is_read_once_for_many_authorized_records(h):
    h.seed(
        [
            h.draft(
                field_key=f"fixture-{i}",
                units=[
                    h.unit(
                        statement="我的门卡放在书房" if i == 0 else f"照片编号{i}放在相册",
                        conditions=[],
                        negations=[],
                    )
                ],
            )
            for i in range(32)
        ]
    )
    statements = []
    transaction = h.store.transaction

    @contextmanager
    def observe():
        with transaction() as db:
            db.set_trace_callback(statements.append)
            yield db

    h.store.transaction = observe
    result = h.select(query="我的门卡放哪儿")
    assert [unit["statement"] for unit in result["selected_units"]] == ["我的门卡放在书房"]
    scans = [q for q in statements if q.startswith("SELECT ") and "FROM search_index" in q]
    assert len(scans) == 1
