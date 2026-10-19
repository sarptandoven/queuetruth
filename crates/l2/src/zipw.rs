//! Minimal ZIP writer for the `.npz` export: STORED (method 0) or DEFLATE (method 8, raw deflate from `flate2`'s
//! pure-Rust miniz_oxide backend at level 6, the zlib default that `numpy.savez_compressed` / `zipfile.ZIP_DEFLATED`
//! use) members, CRC-32 (IEEE 802.3, reflected polynomial `0xEDB88320`) computed in-crate, local file headers, central
//! directory and end-of-central-directory record (APPNOTE 6.3.10). Each member is compressed into memory first, so
//! the CRC and both sizes are known before its local header is written (no data descriptor, general-purpose flags 0).
//! Every timestamp is the DOS epoch 1980-01-01 00:00:00, no extra time fields are written and the compressor settings
//! are fixed, so equal inputs give byte-identical archives. ZIP64 fields are written only when a member (compressed or
//! not) is 4 GiB or larger (or an offset or count overflows the 32/16-bit fields); this writer never extracts.

use std::io::{self, Write};

/// Sentinel of a 32-bit field whose value lives in the ZIP64 extra field / record.
const U32_SENTINEL: u64 = 0xFFFF_FFFF;
/// Sentinel of a 16-bit entry count whose value lives in the ZIP64 end-of-central-directory record.
const U16_SENTINEL: u64 = 0xFFFF;
/// DOS time 00:00:00.
const DOS_TIME: u16 = 0;
/// DOS date 1980-01-01 (`(year - 1980) << 9 | month << 5 | day`).
const DOS_DATE: u16 = (1 << 5) | 1;
/// "Version needed to extract": 2.0 (STORED, DEFLATE) and 4.5 (ZIP64).
const VERSION_20: u16 = 20;
const VERSION_45: u16 = 45;
/// "Version made by": host 3 (UNIX) in the high byte, so the external attributes carry the file mode.
const MADE_BY_UNIX: u16 = 3 << 8;
/// External attributes: regular file, mode 0644.
const EXTERNAL_ATTRS: u32 = 0o100_644 << 16;
/// zlib's default level (`Z_DEFAULT_COMPRESSION`), as used by Python's `zipfile.ZIP_DEFLATED`.
const DEFLATE_LEVEL: u32 = 6;

/// How member data is stored.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Method {
    /// Method 0: the bytes as they are (`np.savez`). Kept and tested; no exporter writes it since npz went DEFLATE.
    #[cfg_attr(
        not(test),
        expect(dead_code, reason = "np.savez-style option, exercised by the zipw tests")
    )]
    Stored,
    /// Method 8: raw deflate, level 6 (`np.savez_compressed`).
    Deflate,
}

impl Method {
    fn code(self) -> u16 {
        match self {
            Method::Stored => 0,
            Method::Deflate => 8,
        }
    }
}

/// The member data as it goes into the archive.
fn encode(method: Method, data: &[u8]) -> io::Result<std::borrow::Cow<'_, [u8]>> {
    match method {
        Method::Stored => Ok(std::borrow::Cow::Borrowed(data)),
        Method::Deflate => {
            let buf = Vec::with_capacity(data.len() / 4 + 64);
            let mut e = flate2::write::DeflateEncoder::new(buf, flate2::Compression::new(DEFLATE_LEVEL));
            e.write_all(data)?;
            Ok(std::borrow::Cow::Owned(e.finish()?))
        }
    }
}

const fn crc_tables() -> [[u32; 256]; 8] {
    let mut t = [[0u32; 256]; 8];
    let mut i = 0;
    while i < 256 {
        let mut c = i as u32;
        let mut k = 0;
        while k < 8 {
            c = if c & 1 != 0 { 0xEDB8_8320 ^ (c >> 1) } else { c >> 1 };
            k += 1;
        }
        t[0][i] = c;
        i += 1;
    }
    let mut i = 0;
    while i < 256 {
        let mut s = 1;
        while s < 8 {
            let prev = t[s - 1][i];
            t[s][i] = (prev >> 8) ^ t[0][(prev & 0xFF) as usize];
            s += 1;
        }
        i += 1;
    }
    t
}

/// Slicing-by-8 tables (8 KiB, built at compile time).
static CRC_TABLES: [[u32; 256]; 8] = crc_tables();

/// CRC-32 (IEEE, as used by ZIP, gzip and PNG) of `data`.
pub(crate) fn crc32(data: &[u8]) -> u32 {
    let t = &CRC_TABLES;
    let mut c = !0u32;
    let mut chunks = data.chunks_exact(8);
    for ch in &mut chunks {
        let lo = c ^ u32::from_le_bytes([ch[0], ch[1], ch[2], ch[3]]);
        c = t[7][(lo & 0xFF) as usize]
            ^ t[6][((lo >> 8) & 0xFF) as usize]
            ^ t[5][((lo >> 16) & 0xFF) as usize]
            ^ t[4][(lo >> 24) as usize]
            ^ t[3][usize::from(ch[4])]
            ^ t[2][usize::from(ch[5])]
            ^ t[1][usize::from(ch[6])]
            ^ t[0][usize::from(ch[7])];
    }
    for &b in chunks.remainder() {
        c = (c >> 8) ^ t[0][((c ^ u32::from(b)) & 0xFF) as usize];
    }
    !c
}

/// Little-endian field writer that counts the bytes it wrote.
struct Out<W: Write> {
    w: W,
    pos: u64,
}

impl<W: Write> Out<W> {
    fn bytes(&mut self, b: &[u8]) -> io::Result<()> {
        self.w.write_all(b)?;
        self.pos += b.len() as u64;
        Ok(())
    }
    fn u16(&mut self, v: u16) -> io::Result<()> {
        self.bytes(&v.to_le_bytes())
    }
    fn u32(&mut self, v: u32) -> io::Result<()> {
        self.bytes(&v.to_le_bytes())
    }
    fn u64(&mut self, v: u64) -> io::Result<()> {
        self.bytes(&v.to_le_bytes())
    }
}

/// `v` as a 32-bit field, or the sentinel when it does not fit (or ZIP64 is forced for the field).
fn field32(v: u64, zip64: bool) -> u32 {
    if zip64 || v >= U32_SENTINEL {
        u32::MAX
    } else {
        // Fits: checked just above.
        u32::try_from(v).unwrap_or(u32::MAX)
    }
}

fn invalid(msg: String) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, msg)
}

/// Where each member's local header starts, and the member's sizes / CRC.
struct Placed<'a> {
    name: &'a str,
    /// Uncompressed size.
    size: u64,
    /// Size of the data in the archive.
    csize: u64,
    crc: u32,
    offset: u64,
    /// Sizes go to the ZIP64 extra field.
    big: bool,
}

/// Writes a ZIP archive of `members` (`(name, bytes)`, in order, each stored with `method`) to `w`; returns the archive
/// length. `force_zip64` writes the ZIP64 fields even when nothing overflows (tests only).
pub(crate) fn write_zip<W: Write>(
    w: W,
    members: &[(&str, &[u8])],
    method: Method,
    force_zip64: bool,
) -> io::Result<u64> {
    let mut o = Out { w, pos: 0 };
    let mut placed = Vec::with_capacity(members.len());
    for &(name, data) in members {
        let name_len =
            u16::try_from(name.len()).map_err(|_| invalid(format!("zip member name of {} bytes", name.len())))?;
        if name.is_empty() || name.starts_with('/') || name.contains('\\') {
            return Err(invalid(format!("zip member name {name:?}")));
        }
        let enc = encode(method, data)?;
        let size = data.len() as u64;
        let csize = enc.len() as u64;
        let big = force_zip64 || size >= U32_SENTINEL || csize >= U32_SENTINEL;
        let p = Placed {
            name,
            size,
            csize,
            crc: crc32(data),
            offset: o.pos,
            big,
        };
        // Local file header.
        o.u32(0x0403_4b50)?;
        o.u16(if big { VERSION_45 } else { VERSION_20 })?;
        o.u16(0)?; // flags: sizes known up front, no data descriptor; names are ASCII
        o.u16(method.code())?;
        o.u16(DOS_TIME)?;
        o.u16(DOS_DATE)?;
        o.u32(p.crc)?;
        o.u32(field32(csize, big))?; // compressed size
        o.u32(field32(size, big))?; // uncompressed size
        o.u16(name_len)?;
        o.u16(if big { 20 } else { 0 })?;
        o.bytes(name.as_bytes())?;
        if big {
            o.u16(0x0001)?;
            o.u16(16)?;
            o.u64(size)?; // uncompressed
            o.u64(csize)?; // compressed
        }
        o.bytes(&enc)?;
        placed.push(p);
    }
    let cd_offset = o.pos;
    for p in &placed {
        let far = force_zip64 || p.offset >= U32_SENTINEL;
        let z64_len: u16 = if p.big { 16 } else { 0 } + if far { 8 } else { 0 };
        let extra = if z64_len > 0 { 4 + z64_len } else { 0 };
        let needed = if p.big || far { VERSION_45 } else { VERSION_20 };
        o.u32(0x0201_4b50)?;
        o.u16(MADE_BY_UNIX | needed)?;
        o.u16(needed)?;
        o.u16(0)?; // flags
        o.u16(method.code())?;
        o.u16(DOS_TIME)?;
        o.u16(DOS_DATE)?;
        o.u32(p.crc)?;
        o.u32(field32(p.csize, p.big))?;
        o.u32(field32(p.size, p.big))?;
        o.u16(u16::try_from(p.name.len()).unwrap_or(u16::MAX))?;
        o.u16(extra)?;
        o.u16(0)?; // comment length
        o.u16(0)?; // disk number start
        o.u16(0)?; // internal attributes
        o.u32(EXTERNAL_ATTRS)?;
        o.u32(field32(p.offset, far))?;
        o.bytes(p.name.as_bytes())?;
        if extra > 0 {
            // ZIP64 extended information: only the fields whose header value is the sentinel, in this order.
            o.u16(0x0001)?;
            o.u16(z64_len)?;
            if p.big {
                o.u64(p.size)?; // uncompressed
                o.u64(p.csize)?; // compressed
            }
            if far {
                o.u64(p.offset)?;
            }
        }
    }
    let cd_size = o.pos - cd_offset;
    let n = placed.len() as u64;
    let zip64_end = force_zip64 || n >= U16_SENTINEL || cd_size >= U32_SENTINEL || cd_offset >= U32_SENTINEL;
    if zip64_end {
        let rec_offset = o.pos;
        // ZIP64 end of central directory record (size field excludes its leading 12 bytes).
        o.u32(0x0606_4b50)?;
        o.u64(44)?;
        o.u16(MADE_BY_UNIX | VERSION_45)?;
        o.u16(VERSION_45)?;
        o.u32(0)?; // this disk
        o.u32(0)?; // disk with the central directory
        o.u64(n)?;
        o.u64(n)?;
        o.u64(cd_size)?;
        o.u64(cd_offset)?;
        // ZIP64 end of central directory locator.
        o.u32(0x0706_4b50)?;
        o.u32(0)?;
        o.u64(rec_offset)?;
        o.u32(1)?; // total disks
    }
    let n16 = if zip64_end || n >= U16_SENTINEL {
        u16::MAX
    } else {
        u16::try_from(n).unwrap_or(u16::MAX)
    };
    o.u32(0x0605_4b50)?;
    o.u16(0)?;
    o.u16(0)?;
    o.u16(n16)?;
    o.u16(n16)?;
    o.u32(field32(cd_size, zip64_end))?;
    o.u32(field32(cd_offset, zip64_end))?;
    o.u16(0)?; // comment length
    o.w.flush()?;
    Ok(o.pos)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn crc32_bitwise(data: &[u8]) -> u32 {
        let mut c = !0u32;
        for &b in data {
            c ^= u32::from(b);
            for _ in 0..8 {
                c = if c & 1 != 0 { 0xEDB8_8320 ^ (c >> 1) } else { c >> 1 };
            }
        }
        !c
    }

    #[test]
    fn crc32_known_answers() {
        // Check values of CRC-32/ISO-HDLC (the "check" string of the CRC catalogue and common vectors).
        assert_eq!(crc32(b""), 0);
        assert_eq!(crc32(b"a"), 0xE8B7_BE43);
        assert_eq!(crc32(b"abc"), 0x3524_41C2);
        assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
        assert_eq!(crc32(b"The quick brown fox jumps over the lazy dog"), 0x414F_A339);
        assert_eq!(crc32(&[0u8; 32]), 0x190A_55AD);
        assert_eq!(crc32(&[0xFFu8; 32]), 0xFF6C_AB0B);
        // Slicing-by-8 against the bitwise definition on every length / alignment of the remainder loop.
        let data: Vec<u8> = (0u32..4099)
            .map(|i| (i.wrapping_mul(2_654_435_761) >> 13) as u8)
            .collect();
        for n in (0..64).chain([1000, 4095, 4096, 4099]) {
            for start in 0..3 {
                let s = &data[start..start + n.min(data.len() - start)];
                assert_eq!(crc32(s), crc32_bitwise(s), "len {} start {start}", s.len());
            }
        }
    }

    fn le16(b: &[u8], o: usize) -> u64 {
        u64::from(u16::from_le_bytes([b[o], b[o + 1]]))
    }
    fn le32(b: &[u8], o: usize) -> u64 {
        u64::from(u32::from_le_bytes(b[o..o + 4].try_into().unwrap()))
    }
    fn le64(b: &[u8], o: usize) -> u64 {
        u64::from_le_bytes(b[o..o + 8].try_into().unwrap())
    }
    fn us(v: u64) -> usize {
        usize::try_from(v).unwrap()
    }

    /// A strict reader of what `write_zip` produces: walks EOCD (and the ZIP64 locator / record when present) to the
    /// central directory, resolves ZIP64 extra fields, checks each local header against its central entry, inflates
    /// DEFLATE members with `flate2::read::DeflateDecoder` (which must consume exactly the compressed size), and checks
    /// the uncompressed size and a bitwise CRC-32. Returns `(name, uncompressed bytes)` and the method of every member.
    fn read_back(z: &[u8]) -> (Vec<(String, Vec<u8>)>, Vec<u16>) {
        let eocd = z.len() - 22;
        assert_eq!(le32(z, eocd), 0x0605_4b50);
        assert_eq!(le16(z, eocd + 20), 0, "no comment");
        let (mut n, mut cd_size, mut cd_off) = (le16(z, eocd + 10), le32(z, eocd + 12), le32(z, eocd + 16));
        if n == 0xFFFF || cd_size == 0xFFFF_FFFF || cd_off == 0xFFFF_FFFF {
            let loc = eocd - 20;
            assert_eq!(le32(z, loc), 0x0706_4b50, "ZIP64 locator");
            let rec = us(le64(z, loc + 8));
            assert_eq!(rec + 56, loc);
            assert_eq!(le32(z, rec), 0x0606_4b50, "ZIP64 EOCD record");
            assert_eq!(le64(z, rec + 4), 44);
            assert_eq!(le64(z, rec + 24), le64(z, rec + 32));
            (n, cd_size, cd_off) = (le64(z, rec + 32), le64(z, rec + 40), le64(z, rec + 48));
            assert_eq!(cd_off + cd_size, rec as u64);
        } else {
            assert_eq!(cd_off + cd_size, eocd as u64);
        }
        let mut out = Vec::new();
        let mut methods = Vec::new();
        let mut p = us(cd_off);
        let mut expect_local = 0u64;
        for _ in 0..n {
            assert_eq!(le32(z, p), 0x0201_4b50);
            assert_eq!(le16(z, p + 8), 0, "flags: no data descriptor");
            let method = u16::try_from(le16(z, p + 10)).unwrap();
            assert!(method == 0 || method == 8, "STORED or DEFLATE");
            assert_eq!((le16(z, p + 12), le16(z, p + 14)), (0, 0x21));
            let crc = le32(z, p + 16);
            let (mut csize, mut usize_, mut off) = (le32(z, p + 20), le32(z, p + 24), le32(z, p + 42));
            let (nl, xl, cl) = (us(le16(z, p + 28)), us(le16(z, p + 30)), us(le16(z, p + 32)));
            assert_eq!(cl, 0);
            let name = String::from_utf8(z[p + 46..p + 46 + nl].to_vec()).unwrap();
            let mut x = p + 46 + nl;
            if xl > 0 {
                assert_eq!(le16(z, x), 1, "ZIP64 extra");
                let mut len = le16(z, x + 2);
                x += 4;
                assert_eq!(us(len) + 4, xl);
                for v in [&mut usize_, &mut csize, &mut off] {
                    if *v == 0xFFFF_FFFF {
                        *v = le64(z, x);
                        x += 8;
                        len -= 8;
                    }
                }
                assert_eq!(len, 0, "no unused ZIP64 bytes");
            }
            if method == 0 {
                assert_eq!(csize, usize_);
            }
            assert_eq!(off, expect_local, "members are contiguous");
            let l = us(off);
            assert_eq!(le32(z, l), 0x0403_4b50);
            assert_eq!(le16(z, l + 6), 0, "local flags");
            assert_eq!(le16(z, l + 8), u64::from(method), "local method = central method");
            assert_eq!((le16(z, l + 10), le16(z, l + 12)), (0, 0x21));
            assert_eq!(le32(z, l + 14), crc);
            let lnl = us(le16(z, l + 26));
            let lxl = us(le16(z, l + 28));
            assert_eq!(&z[l + 30..l + 30 + lnl], name.as_bytes());
            if lxl > 0 {
                assert_eq!((le32(z, l + 18), le32(z, l + 22)), (0xFFFF_FFFF, 0xFFFF_FFFF));
                assert_eq!((le16(z, l + 30 + lnl), le16(z, l + 32 + lnl)), (1, 16));
                assert_eq!(le64(z, l + 34 + lnl), usize_);
                assert_eq!(le64(z, l + 42 + lnl), csize);
            } else {
                assert_eq!((le32(z, l + 18), le32(z, l + 22)), (csize, usize_));
            }
            let d = l + 30 + lnl + lxl;
            let raw = &z[d..d + us(csize)];
            let data = if method == 8 {
                let mut dec = flate2::read::DeflateDecoder::new(raw);
                let mut v = Vec::new();
                std::io::Read::read_to_end(&mut dec, &mut v).unwrap();
                assert_eq!(
                    dec.total_in(),
                    csize,
                    "the deflate stream ends exactly at the compressed size"
                );
                v
            } else {
                raw.to_vec()
            };
            assert_eq!(data.len() as u64, usize_, "uncompressed size");
            assert_eq!(u64::from(crc32_bitwise(&data)), crc);
            expect_local = (d + raw.len()) as u64;
            out.push((name, data));
            methods.push(method);
            p = x + cl;
        }
        assert_eq!(expect_local, cd_off, "central directory follows the last member");
        assert_eq!(p as u64, cd_off + cd_size);
        (out, methods)
    }

    fn zip_bytes(members: &[(&str, &[u8])], method: Method, force_zip64: bool) -> Vec<u8> {
        let mut v = Vec::new();
        let n = write_zip(&mut v, members, method, force_zip64).unwrap();
        assert_eq!(n, v.len() as u64);
        v
    }

    /// xorshift64 bytes: incompressible for deflate.
    fn noise(n: usize) -> Vec<u8> {
        let mut x = 0x9e37_79b9_7f4a_7c15u64;
        (0..n)
            .map(|_| {
                x ^= x << 13;
                x ^= x >> 7;
                x ^= x << 17;
                (x >> 32) as u8
            })
            .collect()
    }

    #[test]
    fn layout_round_trips_and_is_byte_deterministic() {
        let big: Vec<u8> = (0u32..100_000).map(|i| (i % 251) as u8).collect();
        let rnd = noise(70_000);
        let members: [(&str, &[u8]); 4] = [
            ("data.npy", &big),
            ("empty.npy", b""),
            ("b/c.txt", b"hello"),
            ("noise.bin", &rnd),
        ];
        let want: Vec<(String, Vec<u8>)> = members.iter().map(|(n, d)| (n.to_string(), d.to_vec())).collect();
        for method in [Method::Stored, Method::Deflate] {
            for force in [false, true] {
                let a = zip_bytes(&members, method, force);
                assert_eq!(a, zip_bytes(&members, method, force), "byte-deterministic");
                let (back, methods) = read_back(&a);
                assert_eq!(back, want, "{method:?} force_zip64 {force}");
                assert_eq!(methods, vec![method.code(); members.len()]);
            }
        }
        // Exact size of the plain STORED layout: 30 + name + data per local header, 46 + name per central entry, 22 EOCD.
        let a = zip_bytes(&members[..1], Method::Stored, false);
        assert_eq!(a.len(), 30 + 8 + big.len() + 46 + 8 + 22);
        assert_eq!(&a[..4], b"PK\x03\x04");
        // DEFLATE compresses the periodic member and does not blow up the incompressible one (stored blocks).
        let d = zip_bytes(&members[..1], Method::Deflate, false);
        assert!(d.len() < big.len() / 20, "deflated {} of {}", d.len(), big.len());
        let d = zip_bytes(&[("noise.bin", &rnd)], Method::Deflate, false);
        assert!(
            d.len() < 30 + 9 + rnd.len() + rnd.len() / 100 + 46 + 9 + 22,
            "{}",
            d.len()
        );
        // Empty archive: just the EOCD, whatever the method.
        for method in [Method::Stored, Method::Deflate] {
            assert_eq!(
                zip_bytes(&[], method, false),
                b"PK\x05\x06\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0".to_vec()
            );
        }
    }

    /// The compressor output is pinned: a change of flate2 / miniz_oxide (backend, version or level) that would change
    /// the bytes of the published npz files fails here rather than silently changing every artifact hash.
    #[test]
    fn deflate_bytes_are_pinned() {
        // The empty input is one final fixed-Huffman block holding only end-of-block (as zlib writes it).
        assert_eq!(encode(Method::Deflate, b"").unwrap().as_ref(), b"\x03\x00");
        let big: Vec<u8> = (0u32..100_000).map(|i| (i % 251) as u8).collect();
        let mut members: Vec<(&str, &[u8])> = vec![("data.npy", &big)];
        let rnd = noise(5_000);
        members.push(("noise.bin", &rnd));
        let z = zip_bytes(&members, Method::Deflate, false);
        use sha2::Digest;
        assert_eq!(
            hex::encode(sha2::Sha256::digest(&z)),
            "72d9a77ac66cdb0b15d91eff14a7c23b94fe9d387e0c369fd71b63009075eaa1",
            "deflate output changed: {} bytes",
            z.len()
        );
    }

    #[test]
    fn golden_bytes_of_a_tiny_archive() {
        let z = zip_bytes(&[("a", b"x")], Method::Stored, false);
        let mut want = Vec::new();
        // Local header: sig, ver 20, flags 0, STORED, time 0, date 0x21, crc("x"), sizes 1/1, name len 1, extra 0.
        want.extend_from_slice(b"PK\x03\x04\x14\x00\x00\x00\x00\x00\x00\x00\x21\x00");
        want.extend_from_slice(&crc32(b"x").to_le_bytes());
        want.extend_from_slice(b"\x01\x00\x00\x00\x01\x00\x00\x00\x01\x00\x00\x00ax");
        // Central entry: made by UNIX 2.0, needed 2.0, ..., external attrs 0644 regular file, offset 0.
        want.extend_from_slice(b"PK\x01\x02\x14\x03\x14\x00\x00\x00\x00\x00\x00\x00\x21\x00");
        want.extend_from_slice(&crc32(b"x").to_le_bytes());
        want.extend_from_slice(b"\x01\x00\x00\x00\x01\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00");
        want.extend_from_slice(&(0o100_644u32 << 16).to_le_bytes());
        want.extend_from_slice(b"\x00\x00\x00\x00a");
        // EOCD: 1 entry, central directory of 47 bytes at offset 32.
        want.extend_from_slice(b"PK\x05\x06\x00\x00\x00\x00\x01\x00\x01\x00\x2f\x00\x00\x00\x20\x00\x00\x00\x00\x00");
        assert_eq!(z, want);
        assert_eq!(crc32(b"x"), 0x8CDC_1683);
    }

    #[test]
    fn golden_bytes_of_a_tiny_deflate_archive() {
        let z = zip_bytes(&[("a", b"x")], Method::Deflate, false);
        // "x" as one final fixed-Huffman block: literal 'x' then end-of-block (zlib -15 gives the same 3 bytes).
        let stream = b"\xab\x00\x00";
        let mut want = Vec::new();
        // Local header: sig, ver 20, flags 0, DEFLATE (8), time 0, date 0x21, crc("x"), sizes 3/1, name len 1, extra 0.
        want.extend_from_slice(b"PK\x03\x04\x14\x00\x00\x00\x08\x00\x00\x00\x21\x00");
        want.extend_from_slice(&crc32(b"x").to_le_bytes());
        want.extend_from_slice(b"\x03\x00\x00\x00\x01\x00\x00\x00\x01\x00\x00\x00a");
        want.extend_from_slice(stream);
        // Central entry: made by UNIX 2.0, needed 2.0, flags 0, DEFLATE, ..., sizes 3/1, offset 0.
        want.extend_from_slice(b"PK\x01\x02\x14\x03\x14\x00\x00\x00\x08\x00\x00\x00\x21\x00");
        want.extend_from_slice(&crc32(b"x").to_le_bytes());
        want.extend_from_slice(b"\x03\x00\x00\x00\x01\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00");
        want.extend_from_slice(&(0o100_644u32 << 16).to_le_bytes());
        want.extend_from_slice(b"\x00\x00\x00\x00a");
        // EOCD: 1 entry, central directory of 47 bytes at offset 34.
        want.extend_from_slice(b"PK\x05\x06\x00\x00\x00\x00\x01\x00\x01\x00\x2f\x00\x00\x00\x22\x00\x00\x00\x00\x00");
        assert_eq!(z, want);
    }

    #[test]
    fn bad_member_names_are_refused() {
        for name in ["", "/abs", "a\\b"] {
            for method in [Method::Stored, Method::Deflate] {
                let e = write_zip(Vec::new(), &[(name, b"x")], method, false).unwrap_err();
                assert_eq!(e.kind(), io::ErrorKind::InvalidInput);
            }
        }
        let long = "n".repeat(70_000);
        assert!(write_zip(Vec::new(), &[(long.as_str(), b"x")], Method::Deflate, false).is_err());
    }

    /// Archives for the manual numpy / zipfile check (`np.load(p)["data"]`, `zipfile.ZipFile(p).testzip()`), in
    /// `$TMPDIR/qt-l2-zipw`.
    #[test]
    fn writes_npz_samples_for_numpy() {
        let events: Vec<u8> = (0u8..=255).cycle().take(3 * crate::EVENT_SIZE).collect();
        let npy = crate::npy_bytes(&events);
        let dir = std::env::temp_dir().join("qt-l2-zipw");
        std::fs::create_dir_all(&dir).unwrap();
        for (file, method, force) in [
            ("plain.npz", Method::Stored, false),
            ("zip64.npz", Method::Stored, true),
            ("deflate.npz", Method::Deflate, false),
            ("deflate_zip64.npz", Method::Deflate, true),
        ] {
            let f = std::fs::File::create(dir.join(file)).unwrap();
            write_zip(f, &[("data.npy", &npy)], method, force).unwrap();
            let z = std::fs::read(dir.join(file)).unwrap();
            let (back, methods) = read_back(&z);
            assert_eq!(back, vec![("data.npy".to_string(), npy.clone())]);
            assert_eq!(methods, vec![method.code()]);
        }
    }
}
