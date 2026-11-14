"""Single-user SQLite inventory and durable fixture action ledger."""
from __future__ import annotations
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from cards import validate_card, money
from core import now

class Collection:
    def __init__(self,path):
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,timeout=10,isolation_level=None)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS copies(id TEXT PRIMARY KEY, card TEXT NOT NULL, image_hash TEXT UNIQUE,
            cost INTEGER, allow_sale INTEGER NOT NULL, state TEXT NOT NULL, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fixture_actions(id TEXT PRIMARY KEY, listing TEXT UNIQUE NOT NULL,
            total INTEGER NOT NULL, state TEXT NOT NULL, receipt TEXT UNIQUE);
        ''')
    def close(self):self.db.close()
    def add(self,card,image_hash=None,cost=None,allow_sale=False):
        card=validate_card(card,True)
        if type(allow_sale) is not bool:raise ValueError('allow_sale must be boolean.')
        if cost is not None:money(cost,'cost')
        if image_hash is not None and (not isinstance(image_hash,str) or len(image_hash)!=64 or any(c not in '0123456789abcdef' for c in image_hash)):
            raise ValueError('Image hash must be SHA-256 hexadecimal.')
        key='copy-'+uuid.uuid4().hex[:12]
        try:self.db.execute('INSERT INTO copies VALUES(?,?,?,?,?,?,?)',
                (key,json.dumps(card,ensure_ascii=False),image_hash,cost,int(allow_sale),'owned',now()))
        except sqlite3.IntegrityError:raise ValueError('This image is already registered; confirm separate physical copies manually.') from None
        return self.get(key)
    def get(self,key):
        row=self.db.execute('SELECT * FROM copies WHERE id=?',(key,)).fetchone()
        if not row:raise ValueError('Owned copy not found.')
        d=dict(row);d['card']=json.loads(d['card']);d['allow_sale']=bool(d['allow_sale']);return d
    def list(self):return [self.get(r[0]) for r in self.db.execute('SELECT id FROM copies ORDER BY created,id')]
    def reserve_fixture(self,intent,listing,total,budget,reserve):
        for name,value in [('total',total),('budget',budget),('reserve',reserve)]:money(value,name)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.db.execute('SELECT 1 FROM fixture_actions WHERE id=? OR listing=?',(intent,listing)).fetchone():
                raise ValueError('Duplicate or unresolved action; reconcile instead of resubmitting.')
            used=self.db.execute("SELECT COALESCE(SUM(total),0) FROM fixture_actions WHERE state IN ('reserved','unknown','confirmed')").fetchone()[0]
            if used+total>budget-reserve:raise ValueError('Budget or reserve would be exceeded.')
            self.db.execute('INSERT INTO fixture_actions VALUES(?,?,?,?,NULL)',(intent,listing,total,'reserved'))
            self.db.execute('COMMIT')
        except Exception:self.db.execute('ROLLBACK');raise
    def unknown(self,intent):
        self.db.execute("UPDATE fixture_actions SET state='unknown' WHERE id=? AND state='reserved'",(intent,))
    def confirm_fixture(self,intent,receipt):
        if not isinstance(receipt,str) or not receipt.startswith('TEST-'):raise ValueError('Fixture receipt required.')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row=self.db.execute('SELECT * FROM fixture_actions WHERE id=?',(intent,)).fetchone()
            if not row:raise ValueError('Action not found.')
            if row['state']=='confirmed' and row['receipt']!=receipt:raise ValueError('Receipt conflict.')
            self.db.execute("UPDATE fixture_actions SET state='confirmed',receipt=? WHERE id=?",(receipt,intent))
            self.db.execute('COMMIT')
        except Exception:self.db.execute('ROLLBACK');raise
    def actions(self):return [dict(x) for x in self.db.execute('SELECT * FROM fixture_actions ORDER BY id')]
