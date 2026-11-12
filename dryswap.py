#!/usr/bin/env python3
"""dryswap — V2 型のプールの送金・交換を、仮の条件で送信せずに試す。

公開 RPC の eth_call と state override だけを使う。署名も、鍵も、ガス代の支払いも要らない。
仮の持ち主のアドレスに Multicall3 のコードを一時的に載せ、1回の eth_call の中で
  送金 → swap → 受取残高の確認
を順に実行させる。チェーンには何も保存されない。

使い方:
  python3 dryswap.py sell      --rpc URL --pool 0x.. --token-in 0x.. --amount 100
  python3 dryswap.py roundtrip --rpc URL --pool-a 0x.. --pool-b 0x.. --token-in 0x.. --amount 0.001

これは「この条件では止まらなかった／ここで止まった」を見る道具で、利益や安全を保証するものではない。README の「限界」を読むこと。
標準ライブラリだけで動く（Python 3.8 以上）。
"""
import argparse, hashlib, json, re, sys, time, urllib.request, urllib.error
from decimal import Decimal, InvalidOperation

# ---- keccak-256（標準ライブラリの sha3_256 は別物なので、自前の小さな実装）----
_RC = [0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000, 0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
       0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A, 0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
       0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A, 0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008]
_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
_M = (1 << 64) - 1


def _rol(x, n):
    return ((x << n) | (x >> (64 - n))) & _M if n else x


def _f(A):
    for rc in _RC:
        C = [A[x][0] ^ A[x][1] ^ A[x][2] ^ A[x][3] ^ A[x][4] for x in range(5)]
        D = [C[(x - 1) % 5] ^ _rol(C[(x + 1) % 5], 1) for x in range(5)]
        A = [[A[x][y] ^ D[x] for y in range(5)] for x in range(5)]
        B = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                B[y][(2 * x + 3 * y) % 5] = _rol(A[x][y], _ROT[x][y])
        A = [[B[x][y] ^ ((~B[(x + 1) % 5][y]) & B[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
        A[0][0] ^= rc
    return A


def keccak256(data: bytes) -> bytes:
    rate = 136
    p = bytearray(data)
    p.append(0x01)
    while len(p) % rate:
        p.append(0)
    p[-1] |= 0x80
    A = [[0] * 5 for _ in range(5)]
    for off in range(0, len(p), rate):
        for i in range(rate // 8):
            A[i % 5][i // 5] ^= int.from_bytes(p[off + 8 * i: off + 8 * i + 8], 'little')
        A = _f(A)
    return b''.join(A[i % 5][i // 5].to_bytes(8, 'little') for i in range(4))


# ---- 定数 ----
MULTICALL3 = '0xca11bde05977b3631167028862be2a173976ca11'   # 多くのチェーンで同じ住所にある
SIM_USER = '0x00000000000000000000000000000000d2a15a9e'      # 試し実行用の住所（実在の保有者がいないという保証ではない）
CALLER = '0x00000000000000000000000000000000ca11e200'        # eth_call の送り主に使う住所
S_BALANCE_OF, S_TRANSFER, S_GET_RESERVES = '70a08231', 'a9059cbb', '0902f1ac'
S_TOKEN0, S_TOKEN1, S_DECIMALS, S_SYMBOL = '0dfe1681', 'd21220a7', '313ce567', '95d89b41'
S_SWAP, S_AGGREGATE3 = '022c0d9f', '82ad56cb'
OZ_NAMESPACED = 0x52c63247e1f47db19d5ce0460030c497f067ca4cebf71ba98eeadabe20bace00   # OpenZeppelin v5（upgradeable）の ERC20 の置き場所
SLOT_CANDIDATES = list(range(0, 21)) + [51, 52, 101, 102, 151, 201, OZ_NAMESPACED]


def w(v):
    if not isinstance(v, int) or not 0 <= v < 2**256:
        raise Stop('uint256 の範囲外。')
    return format(v, '064x')


def pad(addr):
    return addr[2:].lower().zfill(64)


class Stop(Exception):
    pass


def valid_address(value):
    if not re.fullmatch(r'0x[0-9a-fA-F]{40}', value):
        raise Stop('住所は 0x と40桁の16進数で指定する。')
    return value.lower()


def uint_data(data):
    if len(data) != 32:
        raise Stop('uint256 の応答が32バイトではない。')
    return int.from_bytes(data, 'big')


def hex_bytes(value):
    if not isinstance(value, str) or not re.fullmatch(r'0x(?:[0-9a-fA-F]{2})*', value):
        raise Stop('RPC の16進応答が不正。')
    return bytes.fromhex(value[2:])


def parse_amount(value, decimals):
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise Stop('入力額が数値ではない。')
    if not number.is_finite() or number <= 0:
        raise Stop('入力額は正の有限値で指定する。')
    sign, digits, exponent = number.as_tuple()
    power = exponent + decimals
    if len(digits) + power > 78:
        raise Stop('入力額が uint256 の範囲外。')
    if power < 0:
        if -power >= len(digits) or any(digits[power:]):
            raise Stop('入力額にトークンの最小単位より細かい桁がある。')
        digits = digits[:power]
        power = 0
    result = int(''.join(map(str, digits))) * 10**power
    w(result)
    return result


class Rpc:
    def __init__(self, url, pause=0.2):
        self.url, self.pause, self.calls = url, pause, 0

    def request(self, method, params):
        self.calls += 1
        time.sleep(self.pause)
        body = json.dumps(dict(jsonrpc='2.0', id=1, method=method, params=params)).encode()
        req = urllib.request.Request(self.url, data=body, headers={'content-type': 'application/json', 'User-Agent': 'dryswap/0.1'})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.load(response)
        except urllib.error.HTTPError as e:
            raise Stop(f'RPC が HTTP {e.code} を返した。ここで止める（繰り返さない）。') from e
        except Exception as e:
            raise Stop(f'RPC の通信または応答に失敗（{type(e).__name__}）。繰り返さない。') from e
        if not isinstance(result, dict) or result.get('id') != 1 or result.get('jsonrpc') != '2.0':
            raise Stop('RPC の応答形式または ID が不正。')
        if ('result' in result) == ('error' in result):
            raise Stop('RPC の result/error が不正。')
        return result

    def result(self, method, params):
        r = self.request(method, params)
        if 'result' not in r:
            e = r.get('error')
            raise Stop(f'{method} が失敗: {(e.get("message") if isinstance(e, dict) else e)}')
        return r['result']

    def call(self, to, data, block, override=None):
        """eth_call。成功なら (True, hex)、失敗なら (False, メッセージ)。"""
        params = [{'to': to, 'data': '0x' + data, 'from': CALLER}, block] + ([override] if override else [])
        r = self.request('eth_call', params)
        if 'result' in r:
            return True, r['result']
        e = r.get('error')
        message = (str(e.get('message')) if isinstance(e, dict) else str(e))[:200]
        lower = message.lower()
        if any(x in lower for x in ('rate limit', 'too many requests', 'forbidden', 'unauthorized', 'quota')):
            raise Stop(f'RPC が利用を拒否: {message}')
        if isinstance(e, dict) and (e.get('code') == 3 or 'execution reverted' in lower):
            return False, message
        raise Stop(f'RPC の呼び出しが失敗（swap の判定には使わない）: {message}')


def encode_aggregate3(calls):
    """Multicall3.aggregate3((address target, bool allowFailure, bytes callData)[]) の呼び出しデータ。allowFailure は全部 true。"""
    parts = []
    for target, calldata in calls:
        b = bytes.fromhex(calldata)
        parts.append(pad(target) + w(1) + w(0x60) + w(len(b)) + b.hex() + '00' * ((32 - len(b) % 32) % 32))
    offsets, cur = [], 32 * len(parts)
    for p in parts:
        offsets.append(w(cur))
        cur += len(p) // 2
    return S_AGGREGATE3 + w(0x20) + w(len(parts)) + ''.join(offsets) + ''.join(parts)


def decode_aggregate3(hexdata, n):
    b = hex_bytes(hexdata)

    def word_at(offset):
        if offset < 0 or offset % 32 or offset + 32 > len(b):
            raise Stop('aggregate3 の応答が短いか、offset が不正。')
        return int.from_bytes(b[offset:offset + 32], 'big')

    if word_at(0) != 32 or word_at(32) != n or len(b) < 64 + 32 * n:
        raise Stop('aggregate3 の応答の件数または配列 offset が不正。')
    base, out = 64, []
    for i in range(n):
        rel = word_at(base + 32 * i)
        if rel < 32 * n:
            raise Stop('aggregate3 の tuple offset が不正。')
        off = base + rel
        ok = word_at(off)
        if ok not in (0, 1) or word_at(off + 32) != 64:
            raise Stop('aggregate3 の bool または bytes offset が不正。')
        d0 = off + 64
        ln = word_at(d0)
        if d0 + 32 + ((ln + 31) // 32) * 32 > len(b):
            raise Stop('aggregate3 の bytes が途中で切れている。')
        out.append((bool(ok), b[d0 + 32:d0 + 32 + ln]))
    return out


def check_override_precheck(ok, h):
    """コードを載せた仮の持ち主に、空の aggregate3 を呼んだ返事を調べる。正しい「空の配列」でなければ、この RPC では試せないとして止める。
    原因は決めつけない（state override に対応していない、黙って無視する、返事が壊れている、など）。"""
    try:
        if ok and decode_aggregate3(h, 0) == []:
            return
    except Stop:
        pass
    raise Stop('state override の事前確認に失敗した（コードを載せた住所から、正しい空の返事が返らない）。この RPC では試せない。')


def revert_text(data):
    if len(data) >= 68 and data[:4].hex() == '08c379a0':   # Error(string)
        ln = int.from_bytes(data[36:68], 'big')
        return data[68:68 + ln].decode('utf-8', 'replace')
    return ('0x' + data.hex()[:72]) if data else '（理由なしで revert）'


class Sim:
    """1つのブロックに固定して、仮の持ち主からの一連の呼び出しを試す。"""

    def __init__(self, rpc, block=None):
        self.rpc = rpc
        self.block = block or rpc.result('eth_blockNumber', [])
        if not isinstance(self.block, str) or not re.fullmatch(r'0x[0-9a-fA-F]+', self.block):
            raise Stop('ブロックは16進の番号で指定する。')
        code = rpc.result('eth_getCode', [MULTICALL3, self.block])
        if len(hex_bytes(code)) < 100:
            raise Stop('このチェーンの定番の住所に Multicall3 が無い。この道具では試せない。')
        self.mc3_code = code
        self.mc3_sha256 = hashlib.sha256(hex_bytes(code)).hexdigest()
        self.override = {SIM_USER: {'code': code}}
        # state override が効いているかを先に確かめる。黙って無視する RPC だと、あとの残高の照合が全部はずれて、原因を取り違える
        ok, h = rpc.call(SIM_USER, encode_aggregate3([]), self.block, self.override)
        check_override_precheck(ok, h)

    def view(self, to, data):
        ok, h = self.rpc.call(to, data, self.block)
        if not ok:
            raise Stop(f'読み取りが失敗: {h}')
        return h

    def uint(self, to, data):
        h = self.view(to, data)
        return uint_data(hex_bytes(h))

    def address(self, to, data):
        value = self.uint(to, data)
        if not 0 < value < 2**160:
            raise Stop('トークン住所の応答が不正。')
        return '0x' + format(value, '040x')

    def token_info(self, token):
        info = dict(address=token, decimals=18, symbol='?')
        ok, h = self.rpc.call(token, S_DECIMALS, self.block)
        if not ok:
            raise Stop('decimals を取得できない。18桁と仮定せず止める。')
        info['decimals'] = uint_data(hex_bytes(h))
        if info['decimals'] > 255:
            raise Stop('decimals が範囲外。')
        ok, h = self.rpc.call(token, S_SYMBOL, self.block)
        if ok and len(h) >= 130:
            try:
                n = int(h[66:130], 16)
                info['symbol'] = bytes.fromhex(h[130:130 + n * 2]).decode('utf-8', 'replace')
            except Exception:
                pass
        info['symbol'] = ''.join(c for c in info['symbol'] if c.isprintable())[:80]
        return info

    def give_balance(self, token, known_holder, amount):
        """token の中で、仮の持ち主の残高を amount にする上書きを作る。
        known_holder（残高があると分かっている住所。ふつうはプール）の残高が書いてある場所を探し、同じ作り方で仮の持ち主の場所を出す。
        そのあと、上書きした状態で balanceOf(仮の持ち主) が amount を返すことを確かめる。"""
        want = self.uint(token, S_BALANCE_OF + pad(known_holder))
        if want == 0:
            raise Stop('手掛かりにする住所の残高が 0 で、残高の置き場所を探せない。')
        makers = (lambda a, s: keccak256(bytes.fromhex(pad(a)) + s.to_bytes(32, 'big')),      # Solidity の mapping
                  lambda a, s: keccak256(s.to_bytes(32, 'big') + bytes.fromhex(pad(a))))      # Vyper の HashMap
        for s in SLOT_CANDIDATES:
            for mk in makers:
                v = self.rpc.result('eth_getStorageAt', [token, '0x' + mk(known_holder, s).hex(), self.block])
                if uint_data(hex_bytes(v)) != want:
                    continue
                key = '0x' + mk(SIM_USER, s).hex()
                trial = dict(self.override)
                trial[token] = {'stateDiff': {key: '0x' + w(amount)}}
                ok, h = self.rpc.call(token, S_BALANCE_OF + pad(SIM_USER), self.block, trial)
                if ok and uint_data(hex_bytes(h)) == amount:
                    # A single equality could be the pre-existing balance by chance.
                    probe = amount + 1 if amount < 2**256 - 1 else amount - 1
                    check = dict(self.override)
                    check[token] = {'stateDiff': {key: '0x' + w(probe)}}
                    good, result = self.rpc.call(token, S_BALANCE_OF + pad(SIM_USER), self.block, check)
                    if good and uint_data(hex_bytes(result)) == probe:
                        self.override = trial
                        return s if s < 1000 else 'OpenZeppelin v5 の名前付きの場所'
        raise Stop('トークンの残高の置き場所が見つからない（独自の作りのトークン）。この道具では試せない。')

    def run(self, calls):
        """calls = [(ラベル, 相手, データ)]。仮の持ち主から順に実行して、各段の (成否, 返り値) を返す。"""
        ok, h = self.rpc.call(SIM_USER, encode_aggregate3([(t, d) for _, t, d in calls]), self.block, self.override)
        if not ok:
            if 'argument' in h or 'override' in h.lower():
                raise Stop(f'この RPC は state override を受け付けない: {h}')
            raise Stop(f'通しの呼び出しが失敗: {h}')
        return decode_aggregate3(h, len(calls))


def transfer_ok(result):
    ok, data = result
    return ok and (data == b'' or (len(data) == 32 and int.from_bytes(data, 'big') == 1))


def checked_prefix(calls, results):
    if len(calls) != len(results):
        raise Stop('呼び出しと応答の件数が合わない。')
    for (label, _, data), result in zip(calls, results):
        if not result[0]:
            raise Stop(f'前の段が再現できなかった（{label}）: {revert_text(result[1])}')
        if data.startswith(S_TRANSFER) and not transfer_ok(result):
            raise Stop(f'前の送金が false または不正な返り値（{label}）。')


def read_balance(result):
    if not result[0]:
        raise Stop(f'残高読み取りが失敗: {revert_text(result[1])}')
    return uint_data(result[1])


def leg(sim, prefix, pool, token_in, amount_in, label):
    """固定blockでprefixから毎回再実行し、出力量を近似探索する。次の脚には受取差分だけを渡す。"""
    if amount_in <= 0:
        raise Stop(f'{label}: 入力量が正ではない。')
    t0, t1 = sim.address(pool, S_TOKEN0), sim.address(pool, S_TOKEN1)
    tin = token_in.lower()
    if t0 == t1 or tin not in (t0, t1):
        raise Stop(f'{label}: プールのトークンの組が不正、または入力トークンが含まれない。')
    if any(t in (SIM_USER, CALLER, MULTICALL3) for t in (t0, t1)):
        raise Stop('トークン住所が試し実行用の住所と衝突している。')
    tout = t1 if tin == t0 else t0
    res = hex_bytes(sim.view(pool, S_GET_RESERVES))
    if len(res) < 64 or len(res) % 32:   # 標準は3語（96バイト）。手数料などを足して4語返す派生もあるので、先頭の2語だけ使う
        raise Stop('getReserves の応答が不正。')
    r0, r1 = uint_data(res[:32]), uint_data(res[32:64])
    rin, rout = (r0, r1) if tin == t0 else (r1, r0)
    if rin <= 0 or rout <= 1:
        raise Stop('探索できる残高がない。')
    send = (f'{label}: プールへ送る', tin, S_TRANSFER + pad(pool) + w(amount_in))
    bal_pool = (f'{label}: プールの残高', tin, S_BALANCE_OF + pad(pool))
    bal_user_out = (f'{label}: 受取残高', tout, S_BALANCE_OF + pad(SIM_USER))

    def swap(out):
        a0, a1 = (0, out) if tout == t1 else (out, 0)
        return (f'{label}: swap', pool, S_SWAP + w(a0) + w(a1) + pad(SIM_USER) + w(0x80) + w(0))

    rep = dict(pool=pool, token_in=tin, token_out=tout, sent=amount_in, reserve_in=rin, reserve_out=rout)
    n = len(prefix)
    r = sim.run(prefix + [bal_pool, send, bal_pool])
    checked_prefix(prefix, r[:n])
    if not transfer_ok(r[n + 1]):
        rep.update(stopped_at='送金', reason=revert_text(r[n + 1][1]) if not r[n + 1][0] else 'transfer が false または不正な返り値')
        return rep, None, 0, tout
    received = read_balance(r[n + 2]) - read_balance(r[n])
    rep.update(pool_received=received, pool_received_ratio=received / amount_in)
    if received <= 0:
        rep.update(stopped_at='送金', reason='送ったのに、プールの残高が増えなかった')
        return rep, None, 0, tout
    # 0%手数料の式を探索の打切り値にする。独自の式での真の上限ではない。
    hi = min(rout - 1, max(1, rout * received // (rin + received)))

    def try_out(out):
        rr = sim.run(prefix + [bal_user_out, send, swap(out), bal_user_out])
        checked_prefix(prefix, rr[:n])
        if not transfer_ok(rr[n + 1]):
            raise Stop('探索中の送金が再現できなかった。')
        before = read_balance(rr[n])
        if not rr[n + 2][0]:
            return False, rr[n + 2][1], None, None
        after = read_balance(rr[n + 3])
        if after < before:
            raise Stop('出力トークンの残高が減った。標準の受取として扱えない。')
        return True, rr[n + 2][1], after - before, after

    ok, data, _, _ = try_out(1)
    if not ok:
        rep.update(stopped_at='swap', reason=revert_text(data), note='最小単位1の出力では通らなかった（他の量は未確認）')
        return rep, None, 0, tout
    lo, top = 1, hi + 1
    for _ in range(22):
        mid = (lo + top) // 2
        if mid == lo:
            break
        if try_out(mid)[0]:
            lo = mid
        else:
            top = mid
    ok, data, got, after = try_out(lo)
    if not ok:
        raise Stop('最後のswapが再現できなかった。')
    more = min(rout - 1, lo * 101 // 100 + 1)
    more_ok, over, _, _ = try_out(more) if more > lo else (None, b'', None, None)
    rep.update(max_out=lo, user_received=got, user_balance=after,
               search_cap=hi, search_gap=top-lo, more_probe_out=more if more > lo else None,
               more_probe_ok=more_ok, reason_when_asking_more=revert_text(over) if more_ok is False else None,
               fee_vs_pool_received=1 - lo * rin / (received * (rout - lo)),
               fee_vs_sent=1 - lo * rin / (amount_in * (rout - lo)))
    return rep, prefix + [send, swap(lo)], got, tout


def fmt(v, dec):
    whole, fraction = divmod(v, 10**dec)
    tail = (str(fraction).zfill(dec) if dec else '').rstrip('0')
    return f'{whole:,}' + ('.' + tail if tail else '')


def print_leg(rep, tin, tout):
    print(f"  プール {rep['pool']}")
    print(f"  送った量: {fmt(rep['sent'], tin['decimals'])} {tin['symbol']}")
    if 'pool_received' in rep:
        print(f"  プールに届いた量: {fmt(rep['pool_received'], tin['decimals'])} {tin['symbol']}（送った量の {rep['pool_received_ratio'] * 100:.2f}%）")
    if rep.get('stopped_at'):
        print(f"  → {rep['stopped_at']} の段でエラー: {rep['reason']}" + (f"（{rep['note']}）" if rep.get('note') else ''))
        return
    print(f"  swap が通った出力（探索で選んだ量、最大の保証なし）: {fmt(rep['max_out'], tout['decimals'])} {tout['symbol']}")
    print(f"  仮の持ち主に届いた量（残高差）: {fmt(rep['user_received'], tout['decimals'])} {tout['symbol']}")
    if rep.get('fee_vs_pool_received') is not None:
        print(f"  x*y=k の式から逆算した実効の手数料: プールに届いた量に対して {rep['fee_vs_pool_received'] * 100:.2f}%、送った量に対して {rep['fee_vs_sent'] * 100:.2f}%")
    if rep['more_probe_ok'] is False:
        print(f"  それより多く求めたときのエラー: {rep['reason_when_asking_more']}")
    elif rep['more_probe_ok'] is True:
        print('  追加で試したより大きい出力も通った。表示値は真の上限ではない。')


def main():
    ap = argparse.ArgumentParser(description='V2 型のプールでの売り買いを、何も送らずに試す（eth_call + state override）。')
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('sell', 'roundtrip'):
        p = sub.add_parser(name)
        p.add_argument('--rpc', required=True, help='公開 RPC の URL（state override に対応していること）')
        p.add_argument('--token-in', required=True, help='最初に入れるトークンの住所')
        p.add_argument('--amount', required=True, help='入れる量（トークンの単位。例: 0.001）')
        p.add_argument('--block', help='固定するブロック（16進。省略時は最新）')
        p.add_argument('--json', action='store_true', help='結果を JSON で出す')
        if name == 'sell':
            p.add_argument('--pool', required=True)
        else:
            p.add_argument('--pool-a', required=True, help='1つ目のプール（token-in を入れる）')
            p.add_argument('--pool-b', required=True, help='2つ目のプール（受け取ったトークンを入れて、token-in に戻す）')
    a = ap.parse_args()
    try:
        a.token_in = valid_address(a.token_in)
        pools = [valid_address(a.pool)] if a.cmd == 'sell' else [valid_address(a.pool_a), valid_address(a.pool_b)]
        if len(set(pools)) != len(pools):
            raise Stop('往復には異なる2つのプールを指定する。')
        if any(v in (SIM_USER, CALLER, MULTICALL3) for v in pools + [a.token_in]):
            raise Stop('入力住所が試し実行用の住所と衝突している。')
        if a.block is not None and not re.fullmatch(r'0x[0-9a-fA-F]+', a.block):
            raise Stop('ブロックは16進の番号で指定する。')
        rpc = Rpc(a.rpc)
        sim = Sim(rpc, a.block)
        tin = sim.token_info(a.token_in.lower())
        amount = parse_amount(a.amount, tin['decimals'])
        slot = sim.give_balance(tin['address'], pools[0], amount)
        out = dict(block=int(sim.block, 16), token_in=tin, amount=amount, balance_slot=slot, multicall3_code_sha256=sim.mc3_sha256,
                   simulation_user=SIM_USER, caller=CALLER, legs=[])
        prefix, cur_token, cur_amount = [], tin['address'], amount
        infos = [tin]
        for i, pool in enumerate(pools):
            rep, prefix2, got, tout = leg(sim, prefix, pool, cur_token, cur_amount, f'脚{i + 1}')
            infos.append(sim.token_info(tout))
            out['legs'].append(rep)
            if prefix2 is None:
                break
            prefix, cur_token, cur_amount = prefix2, tout, got
        else:
            if a.cmd == 'roundtrip':
                if cur_token != tin['address']:
                    raise Stop('2つ目のプールから出てくるのが、最初のトークンではない。')
                out['end_balance'] = rep['user_balance']
                out['change_pct'] = (out['end_balance'] - amount) / amount * 100
        out['rpc_calls'] = rpc.calls
    except Stop as e:
        print(f'止めた: {e}', file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    print(f"ブロック {out['block']}（この1つのブロックの状態で試した）。仮の持ち主に {fmt(amount, tin['decimals'])} {tin['symbol']} を置いた。")
    for i, rep in enumerate(out['legs']):
        print(f'脚{i + 1}:')
        print_leg(rep, infos[i], infos[i + 1])
    if 'change_pct' in out:
        print(f"往復のあとの残高: {fmt(out['end_balance'], tin['decimals'])} {tin['symbol']}（最初の量に対して {out['change_pct']:+.3f}%。ガス代や橋の費用は入っていない）")
    print(f"RPC の呼び出し {out['rpc_calls']} 回。何も送っていない。これは「この条件では止まらなかった／ここで止まった」の確認で、利益や安全の保証ではない。")
    return 0


if __name__ == '__main__':
    sys.exit(main())
