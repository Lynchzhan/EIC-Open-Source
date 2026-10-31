"""明确持有共享读锁，诊断真实连接hook的journal转换。"""
import json
import os
from pathlib import Path
import sqlite3
import time

import pytest

from eic.core.database import _sqlite_on_connect


@pytest.mark.parametrize('journal_mode',['delete','wal'])
def test_runtime_hook_preserves_journal_and_live_reader(tmp_path,journal_mode):
    path=tmp_path/'controlled-reader.db'
    with sqlite3.connect(path) as seed:
        assert seed.execute('PRAGMA journal_mode='+journal_mode).fetchone()[0]==journal_mode
        seed.execute('CREATE TABLE synthetic(value INTEGER)')
        seed.execute('INSERT INTO synthetic VALUES(1)')
    reader=sqlite3.connect(path,timeout=30)
    starter=sqlite3.connect(path,timeout=30)
    report={'scope':'synthetic reader-held lock; not historical lock-owner attribution',
            'readerJournalBefore':reader.execute('PRAGMA journal_mode').fetchone()[0]}
    try:
        reader.execute('BEGIN')
        assert reader.execute('SELECT value FROM synthetic').fetchall()==[(1,)]
        report['readerInTransactionBefore']=reader.in_transaction
        report['starterInTransactionBefore']=starter.in_transaction
        began=time.monotonic()
        _sqlite_on_connect(starter,None)
        report['elapsedSeconds']=time.monotonic()-began
        report['readerInTransactionAfter']=reader.in_transaction
        report['starterInTransactionAfter']=starter.in_transaction
        assert reader.in_transaction
        assert not starter.in_transaction
        assert starter.execute('PRAGMA journal_mode').fetchone()[0]==journal_mode
        assert starter.execute('PRAGMA foreign_keys').fetchone()[0]==1
        assert starter.execute('PRAGMA busy_timeout').fetchone()[0]==30000
        assert starter.execute('PRAGMA synchronous').fetchone()[0]==1
        assert reader.execute('SELECT value FROM synthetic').fetchall()==[(1,)]
    finally:
        reader.rollback()
        report['journalAfterRelease']=starter.execute('PRAGMA journal_mode').fetchone()[0]
        reader.close()
        starter.close()
        # 独立运行时保存在本用例临时目录；外置父子保护器可另指定收据目录。
        evidence_root = Path(os.environ.get('PAIR_CHILD_GUARD_DIR', str(tmp_path)))
        evidence_root.mkdir(parents=True, exist_ok=True)
        dest=evidence_root/('reader-regression-'+journal_mode+'.json')
        dest.write_text(json.dumps(report,indent=2),encoding='utf-8')
