"""Bound a canonical persisted request without decoding its historical model.

The raw SHA256 must equal the digest of the originally admitted canonical JSON.
This is a bounded top-level value scanner, not a replacement JSON parser. It
hashes every byte (including skipped values) and compares the three binding
values to the live completed job. Noncanonical/changed bytes fail closed.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .order_feed_io import directory


CHUNK = 64 * 1024
MAX_BYTES = 128 * 1024 * 1024
TOKENS = re.compile(rb'\\.?|["{}\[\],]')


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _canonical_digest(value):
    result=hashlib.sha256()
    encoder=json.JSONEncoder(ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
    for part in encoder.iterencode(value):result.update(part.encode())
    return result.hexdigest()


class _Reader:
    def __init__(self, fd):
        self.fd=fd;self.buffer=b'';self.pos=0;self.total=0;self.hash=hashlib.sha256()

    def available(self):
        if self.pos==len(self.buffer):
            self.buffer=os.read(self.fd,CHUNK);self.pos=0
            self.total+=len(self.buffer)
            if self.total>MAX_BYTES:raise ValueError('request too large')
            self.hash.update(self.buffer)
        return self.pos<len(self.buffer)

    def take(self):
        if not self.available():raise ValueError('incomplete request')
        value=self.buffer[self.pos];self.pos+=1;return value

    def expect(self, value):
        if self.take()!=value:raise ValueError('noncanonical request structure')

    def key(self):
        self.expect(34);raw=bytearray(b'"');escape=False
        while True:
            char=self.take();raw.append(char)
            if len(raw)>1024:raise ValueError('request key too long')
            if escape:escape=False
            elif char==92:escape=True
            elif char==34:break
        key=json.loads(raw)
        if type(key) is not str:raise ValueError('invalid request key')
        return key

    def value_digest(self):
        result=hashlib.sha256();stack=[];quoted=False;carry_escape=False;count=0
        while self.available():
            start=self.pos;scan=start
            if carry_escape:
                scan+=1;carry_escape=False
            for match in TOKENS.finditer(self.buffer,scan):
                token=match.group()
                if token.startswith(b'\\'):
                    if not quoted:raise ValueError('escape outside string')
                    if len(token)==1:carry_escape=True
                elif token==b'"':quoted=not quoted
                elif quoted:continue
                elif token in (b'{',b'['):
                    stack.append(token)
                    if len(stack)>256:raise ValueError('request nesting too deep')
                elif token in (b'}',b']'):
                    if not stack:
                        if token!=b'}':raise ValueError('unexpected array close')
                        end=match.start();result.update(self.buffer[start:end]);count+=end-start
                        self.pos=end
                        if not count:raise ValueError('empty request value')
                        return result.hexdigest()
                    if stack.pop()!={b'}':b'{',b']':b'['}[token]:raise ValueError('unbalanced request')
                elif token==b',' and not stack:
                    end=match.start();result.update(self.buffer[start:end]);count+=end-start
                    self.pos=end
                    if not count:raise ValueError('empty request value')
                    return result.hexdigest()
            result.update(self.buffer[start:]);count+=len(self.buffer)-start;self.pos=len(self.buffer)
        raise ValueError('incomplete request value')


def matches(path, *, request_digest, job_id, roots, file_plans):
    """No writes; caller also guards the pathname before/after this read."""
    path=Path(path)
    expected={key:_canonical_digest(value) for key,value in
              (('job_id',job_id),('roots',roots),('file_plans',file_plans))}
    with directory(path.parent) as parent:
        fd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        try:
            before=os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>MAX_BYTES:
                return False
            reader=_Reader(fd);reader.expect(123);found={};seen=set()
            while True:
                key=reader.key()
                if key in seen or len(seen)>=64:return False
                seen.add(key);reader.expect(58);value=reader.value_digest()
                if key in expected:found[key]=value
                separator=reader.take()
                if separator==125:break
                if separator!=44:return False
            if reader.available():return False
            return (found==expected and reader.hash.hexdigest()==request_digest and
                    _signature(before)==_signature(os.fstat(fd)))
        except (ValueError,TypeError,UnicodeError):
            return False
        finally:
            os.close(fd)
