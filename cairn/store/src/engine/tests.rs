use std::io::Read;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use super::*;

fn put(e: &Engine, bucket: &str, key: &str, data: &[u8], cond: &Condition) -> Result<()> {
    put_v(e, bucket, key, data, cond).map(|_| ())
}

fn put_v(e: &Engine, bucket: &str, key: &str, data: &[u8], cond: &Condition) -> Result<Object> {
    let mut w = e.new_blob()?;
    w.write(data)?;
    let etag = hex::encode(w.md5());
    let seg = w.seal()?;
    e.put_object(
        bucket,
        Object { key: key.into(), size: seg.size, etag, modified: now_ms(), segments: vec![seg], ..Default::default() },
        cond,
    )
}

fn read(e: &Engine, bucket: &str, key: &str) -> Vec<u8> {
    let o = e.get_object(bucket, key).unwrap();
    let mut out = vec![];
    e.reader(&o, 0, o.size, None).unwrap().read_to_end(&mut out).unwrap();
    out
}

fn blob_count(dir: &Path) -> usize {
    let mut n = 0;
    let mut stack = vec![dir.join("blobs")];
    while let Some(d) = stack.pop() {
        for ent in fs::read_dir(d).unwrap() {
            let ent = ent.unwrap();
            if ent.file_type().unwrap().is_dir() {
                stack.push(ent.path());
            } else {
                n += 1;
            }
        }
    }
    n
}

#[test]
fn replace_and_delete_collect_old_blobs() {
    let dir = tempfile::tempdir().unwrap();
    let e = Engine::open(dir.path(), true).unwrap();
    e.create_bucket("b").unwrap();
    assert!(matches!(e.create_bucket("b"), Err(Error::BucketExists)));
    put(&e, "b", "k", b"one", &Condition::default()).unwrap();
    put(&e, "b", "k", b"two", &Condition::default()).unwrap();
    assert_eq!(read(&e, "b", "k"), b"two");
    assert_eq!(blob_count(dir.path()), 1, "the replaced blob is collected");
    assert!(matches!(e.delete_bucket("b"), Err(Error::BucketNotEmpty)));
    e.delete_object("b", "k", None).unwrap();
    e.delete_object("b", "k", None).unwrap(); // deleting a missing key succeeds
    assert_eq!(blob_count(dir.path()), 0);
    e.delete_bucket("b").unwrap();
    assert!(matches!(e.head_bucket("b"), Err(Error::NoSuchBucket)));
}

#[test]
fn conditional_create_has_exactly_one_winner() {
    let dir = tempfile::tempdir().unwrap();
    let e = Arc::new(Engine::open(dir.path(), true).unwrap());
    e.create_bucket("b").unwrap();
    let (wins, losses) = (Arc::new(AtomicUsize::new(0)), Arc::new(AtomicUsize::new(0)));
    let threads: Vec<_> = (0..32)
        .map(|i| {
            let (e, wins, losses) = (e.clone(), wins.clone(), losses.clone());
            std::thread::spawn(move || {
                let cond = Condition { if_none_match: Some("*".into()), ..Default::default() };
                match put(&e, "b", "metadata/v1.json", format!("writer {i}").as_bytes(), &cond) {
                    Ok(()) => wins.fetch_add(1, Ordering::SeqCst),
                    Err(Error::PreconditionFailed) => losses.fetch_add(1, Ordering::SeqCst),
                    Err(err) => panic!("{err}"),
                };
            })
        })
        .collect();
    for t in threads {
        t.join().unwrap();
    }
    assert_eq!((wins.load(Ordering::SeqCst), losses.load(Ordering::SeqCst)), (1, 31));
    assert_eq!(blob_count(dir.path()), 1, "losing writers leave no blobs");
    let o = e.get_object("b", "metadata/v1.json").unwrap();
    let wrong = Condition { if_match: Some("\"nope\"".into()), ..Default::default() };
    assert!(matches!(put(&e, "b", "metadata/v1.json", b"x", &wrong), Err(Error::PreconditionFailed)));
    let right = Condition { if_match: Some(format!("\"{}\"", o.etag)), ..Default::default() };
    put(&e, "b", "metadata/v1.json", b"x", &right).unwrap();
    let any = Condition { if_match: Some("*".into()), ..Default::default() };
    assert!(matches!(put(&e, "b", "missing", b"x", &any), Err(Error::NoSuchKey)));
}

#[test]
fn recovery_after_crash() {
    let dir = tempfile::tempdir().unwrap();
    {
        let e = Engine::open(dir.path(), true).unwrap();
        e.create_bucket("b").unwrap();
        put(&e, "b", "kept", b"safe", &Condition::default()).unwrap();
        // Crash 1: a write in progress (temporary file only); leak it so its cleanup never runs.
        let mut w = e.new_blob().unwrap();
        w.write(b"half-written").unwrap();
        std::mem::forget(w);
        // Crash 2: a blob sealed but never committed to the index.
        let mut w2 = e.new_blob().unwrap();
        w2.write(b"orphan").unwrap();
        w2.seal().unwrap();
    } // the process dies here: neither write is visible
    let e = Engine::open(dir.path(), true).unwrap();
    let rep = e.recover().unwrap();
    assert_eq!((rep.objects_with_missing_blobs, rep.blobs_referenced), (0, 1));
    assert_eq!(read(&e, "b", "kept"), b"safe");
    assert_eq!(blob_count(dir.path()), 1, "orphan blob removed");
    assert_eq!(fs::read_dir(dir.path().join("tmp")).unwrap().count(), 0, "temporary files removed");
    // Lost data is reported, not hidden.
    let o = e.get_object("b", "kept").unwrap();
    fs::remove_file(e.blob_path(&o.segments[0].blob)).unwrap();
    assert_eq!(e.recover().unwrap().objects_with_missing_blobs, 1);
}

#[test]
fn listing_with_delimiter_and_pages() {
    let dir = tempfile::tempdir().unwrap();
    let e = Engine::open(dir.path(), false).unwrap();
    e.create_bucket("b").unwrap();
    for k in ["a/1", "a/2", "a/b/1", "a/b/2", "a/c", "b", "c/x/y", "c/z"] {
        put(&e, "b", k, k.as_bytes(), &Condition::default()).unwrap();
    }
    let r = e.list("b", &ListOptions { prefix: "a/".into(), delimiter: "/".into(), max_keys: 1000, ..Default::default() }).unwrap();
    assert_eq!(r.objects.iter().map(|o| o.key.as_str()).collect::<Vec<_>>(), ["a/1", "a/2", "a/c"]);
    assert_eq!(r.common_prefixes, ["a/b/"]);
    // One entry per page with a delimiter: every entry exactly once, in order.
    let (mut seen, mut after) = (vec![], String::new());
    loop {
        let r = e.list("b", &ListOptions { delimiter: "/".into(), after: after.clone(), max_keys: 1, ..Default::default() }).unwrap();
        seen.extend(r.objects.iter().map(|o| o.key.clone()));
        seen.extend(r.common_prefixes.clone());
        if !r.truncated {
            break;
        }
        after = r.next_after;
    }
    assert_eq!(seen, ["a/", "b", "c/"]);
    assert_eq!(e.list("b", &ListOptions { prefix: "c/".into(), max_keys: 1000, ..Default::default() }).unwrap().objects.len(), 2);
    assert!(matches!(e.list("missing", &ListOptions { max_keys: 1, ..Default::default() }), Err(Error::NoSuchBucket)));
}

#[test]
fn multipart_assembles_parts_without_copying() {
    let dir = tempfile::tempdir().unwrap();
    let e = Engine::open(dir.path(), false).unwrap();
    e.create_bucket("b").unwrap();
    let u = e.create_upload(Upload { bucket: "b".into(), key: "big".into(), ..Default::default() }).unwrap();
    let part = |n: u32, data: &[u8]| -> Part {
        let mut w = e.new_blob().unwrap();
        w.write(data).unwrap();
        let md5 = w.md5();
        let seg = w.seal().unwrap();
        let p = Part { number: n, blob: seg.blob, size: seg.size, etag: hex::encode(&md5), md5, ..Default::default() };
        e.put_part("b", "big", &u.id, p.clone()).unwrap();
        p
    };
    let p1 = part(1, &vec![b'a'; MIN_PART_SIZE as usize]);
    part(2, b"replaced");
    part(2, &vec![b'b'; MIN_PART_SIZE as usize]); // a re-uploaded part replaces the first upload
    let p3 = part(3, b"tail");
    let noop = |_: &Upload, _: &[Part]| Ok((BTreeMap::new(), String::new()));
    let cp = |n: u32, etag: &str| CompletePart { number: n, etag: etag.into() };
    assert!(matches!(
        e.complete_upload("b", "big", &u.id, &[cp(3, &p3.etag), cp(1, &p1.etag)], &Condition::default(), noop),
        Err(Error::InvalidPartOrder)
    ));
    assert!(matches!(
        e.complete_upload("b", "big", &u.id, &[cp(3, &p3.etag), cp(4, "")], &Condition::default(), noop),
        Err(Error::InvalidPart)
    ));
    let obj = e.complete_upload("b", "big", &u.id, &[cp(1, &p1.etag), cp(3, &p3.etag)], &Condition::default(), noop).unwrap();
    assert_eq!(obj.size, MIN_PART_SIZE + 4);
    assert!(obj.etag.ends_with("-2"));
    assert_eq!(blob_count(dir.path()), 2, "unused parts collected");
    let mut out = vec![];
    e.reader(&obj, MIN_PART_SIZE - 2, 5, None).unwrap().read_to_end(&mut out).unwrap(); // a range spanning both parts
    assert_eq!(out, b"aatai");
    assert!(matches!(e.get_upload("b", "big", &u.id), Err(Error::NoSuchUpload)));
}

#[test]
fn reads_survive_concurrent_replace() {
    let dir = tempfile::tempdir().unwrap();
    let e = Arc::new(Engine::open(dir.path(), false).unwrap());
    e.create_bucket("b").unwrap();
    put(&e, "b", "k", &b"v0".repeat(1000), &Condition::default()).unwrap();
    let stop = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let writer = {
        let (e, stop) = (e.clone(), stop.clone());
        std::thread::spawn(move || {
            let mut i = 1;
            while !stop.load(Ordering::SeqCst) {
                put(&e, "b", "k", &format!("v{}", i % 10).repeat(1000).into_bytes(), &Condition::default()).unwrap();
                i += 1;
            }
        })
    };
    for _ in 0..300 {
        let (obj, mut r) = match e.get_fresh("b", "k", None, |o: &Object| Ok::<_, ()>((0, o.size, None))) {
            Ok(v) => v,
            Err(_) => panic!("read failed"),
        };
        let mut out = vec![];
        r.read_to_end(&mut out).unwrap();
        assert_eq!(out.len() as u64, obj.size);
        assert!(out.chunks(2).all(|c| c == &out[..2]), "torn read");
    }
    stop.store(true, Ordering::SeqCst);
    writer.join().unwrap();
}

fn read_v(e: &Engine, bucket: &str, key: &str, v: &str) -> Vec<u8> {
    let o = e.get_version(bucket, key, Some(v)).unwrap();
    let mut out = vec![];
    e.reader(&o, 0, o.size, None).unwrap().read_to_end(&mut out).unwrap();
    out
}

#[test]
fn versioning_keeps_old_versions_and_delete_markers() {
    let dir = tempfile::tempdir().unwrap();
    let e = Engine::open(dir.path(), false).unwrap();
    e.create_bucket("b").unwrap();
    let null = put_v(&e, "b", "k", b"zero", &Condition::default()).unwrap();
    assert_eq!(null.version(), "null");
    e.set_versioning("b", "Enabled").unwrap();
    let v1 = put_v(&e, "b", "k", b"one", &Condition::default()).unwrap();
    let v2 = put_v(&e, "b", "k", b"two", &Condition::default()).unwrap();
    assert!(!v1.version_id.is_empty() && v1.version_id != v2.version_id);
    assert_eq!(read(&e, "b", "k"), b"two");
    assert_eq!(read_v(&e, "b", "k", &v1.version_id), b"one");
    assert_eq!(read_v(&e, "b", "k", "null"), b"zero");
    let d = e.delete_object("b", "k", None).unwrap();
    assert!(d.delete_marker);
    assert!(matches!(e.get_object("b", "k"), Err(Error::NoSuchKey)));
    assert_eq!(e.latest_delete_marker("b", "k").unwrap(), d.version_id);
    let list = e.list_versions("b", "", "", "", "", 100).unwrap();
    let got: Vec<(String, bool, bool)> =
        list.entries.iter().map(|x| (x.obj.version().to_string(), x.latest, x.obj.delete_marker)).collect();
    assert_eq!(
        got,
        vec![
            (d.version_id.clone().unwrap(), true, true),
            (v2.version_id.clone(), false, false),
            (v1.version_id.clone(), false, false),
            ("null".into(), false, false)
        ]
    );
    // Removing the delete marker brings back the newest version.
    e.delete_version("b", "k", d.version_id.as_ref().unwrap(), false).unwrap();
    assert_eq!(read(&e, "b", "k"), b"two");
    // Permanently deleting the current version promotes the previous one, and frees its blobs.
    let before = blob_count(dir.path());
    e.delete_version("b", "k", &v2.version_id, false).unwrap();
    assert_eq!(read(&e, "b", "k"), b"one");
    assert_eq!(blob_count(dir.path()), before - 1);
    // Pagination over versions.
    let p1 = e.list_versions("b", "", "", "", "", 1).unwrap();
    assert!(p1.truncated);
    let p2 = e.list_versions("b", "", "", &p1.next_key, &p1.next_version, 10).unwrap();
    assert_eq!(p2.entries.iter().map(|x| x.obj.version().to_string()).collect::<Vec<_>>(), vec!["null".to_string()]);
    // A bucket with versions is not empty; recovery keeps version blobs.
    e.delete_version("b", "k", &v1.version_id, false).unwrap();
    assert!(matches!(e.delete_bucket("b"), Err(Error::BucketNotEmpty)));
    let rep = e.recover().unwrap();
    assert_eq!((rep.unreferenced_blobs_removed, rep.objects_with_missing_blobs), (0, 0));
    assert_eq!(read(&e, "b", "k"), b"zero");
    e.delete_version("b", "k", "null", false).unwrap();
    e.delete_bucket("b").unwrap();
}

#[test]
fn suspended_versioning_replaces_the_null_version() {
    let dir = tempfile::tempdir().unwrap();
    let e = Engine::open(dir.path(), false).unwrap();
    e.create_bucket("b").unwrap();
    e.set_versioning("b", "Enabled").unwrap();
    let v1 = put_v(&e, "b", "k", b"one", &Condition::default()).unwrap();
    e.set_versioning("b", "Suspended").unwrap();
    assert_eq!(put_v(&e, "b", "k", b"n1", &Condition::default()).unwrap().version(), "null");
    put_v(&e, "b", "k", b"n2", &Condition::default()).unwrap();
    let vs: Vec<String> = e.list_versions("b", "", "", "", "", 100).unwrap().entries.iter().map(|x| x.obj.version().to_string()).collect();
    assert_eq!(vs, vec!["null".to_string(), v1.version_id.clone()]);
    assert_eq!(read(&e, "b", "k"), b"n2");
    // A delete in a suspended bucket replaces the null version with a null delete marker.
    let d = e.delete_object("b", "k", None).unwrap();
    assert_eq!(d.version_id.as_deref(), Some("null"));
    let vs = e.list_versions("b", "", "", "", "", 100).unwrap();
    assert_eq!(vs.entries.len(), 2);
    assert!(vs.entries[0].obj.delete_marker && vs.entries[0].latest);
    assert_eq!(e.recover().unwrap().unreferenced_blobs_removed, 0);
}

#[test]
fn object_lock_is_enforced_by_the_engine() {
    let dir = tempfile::tempdir().unwrap();
    let e = Engine::open(dir.path(), false).unwrap();
    e.create_bucket_with("b", "Enabled", &[("object-lock", b"{}")]).unwrap();
    let mut w = e.new_blob().unwrap();
    w.write(b"x").unwrap();
    let seg = w.seal().unwrap();
    let far = now_ms() + 3_600_000;
    let o = Object {
        key: "k".into(),
        size: 1,
        segments: vec![seg],
        lock: Lock { mode: "COMPLIANCE".into(), until: far, legal_hold: false },
        ..Default::default()
    };
    let v = e.put_object("b", o, &Condition::default()).unwrap();
    assert!(matches!(e.delete_version("b", "k", &v.version_id, true), Err(Error::ObjectLocked)));
    // A plain delete only adds a delete marker; the locked version stays.
    e.delete_object("b", "k", None).unwrap();
    assert!(e.get_version("b", "k", Some(&v.version_id)).is_ok());
    // Governance gives way with bypass; legal hold never does.
    e.update_object("b", "k", Some(&v.version_id), |o| {
        o.lock = Lock { mode: "GOVERNANCE".into(), until: far, legal_hold: true };
        Ok(())
    })
    .unwrap();
    assert!(matches!(e.delete_version("b", "k", &v.version_id, true), Err(Error::ObjectLocked)));
    e.update_object("b", "k", Some(&v.version_id), |o| {
        o.lock.legal_hold = false;
        Ok(())
    })
    .unwrap();
    assert!(matches!(e.delete_version("b", "k", &v.version_id, false), Err(Error::ObjectLocked)));
    e.delete_version("b", "k", &v.version_id, true).unwrap();
}

#[test]
fn encrypted_blobs_round_trip_and_read_any_range() {
    let dir = tempfile::tempdir().unwrap();
    let e = Engine::open(dir.path(), false).unwrap();
    e.create_bucket("b").unwrap();
    let key = crypt::random_key();
    let data: Vec<u8> = (0..(crypt::CHUNK * 3 + 777)).map(|i| (i % 251) as u8).collect();
    let mut w = e.new_blob_with(Some(&key)).unwrap();
    for piece in data.chunks(10_000) {
        w.write(piece).unwrap();
    }
    let seg = w.seal().unwrap();
    assert!(!seg.nonce.is_empty() && seg.size == data.len() as u64);
    let on_disk = fs::read(e.blob_path(&seg.blob)).unwrap();
    assert_eq!(on_disk.len() as u64, crypt::sealed_len(data.len() as u64));
    assert!(!on_disk.windows(64).any(|w| w == &data[1000..1064])); // no plaintext on disk
    let o = Object { key: "k".into(), size: seg.size, segments: vec![seg], ..Default::default() };
    for (off, n) in [(0u64, data.len() as u64), (crypt::CHUNK as u64 - 5, 10), (1, 1), (crypt::CHUNK as u64 * 3, 777), (12345, 70000)] {
        let mut out = vec![];
        e.reader(&o, off, n, Some(&key)).unwrap().read_to_end(&mut out).unwrap();
        assert_eq!(out, &data[off as usize..(off + n) as usize], "range {off}+{n}");
    }
    assert!(e.reader(&o, 0, 10, None).is_err());
    let mut out = vec![];
    assert!(e.reader(&o, 0, 10, Some(&crypt::random_key())).unwrap().read_to_end(&mut out).is_err());
    // Tampering with the stored bytes is detected.
    let mut bad = on_disk.clone();
    bad[crypt::CHUNK + crypt::TAG + 3] ^= 0xff;
    fs::write(e.blob_path(&o.segments[0].blob), &bad).unwrap();
    let mut out = vec![];
    assert!(e.reader(&o, 0, o.size, Some(&key)).unwrap().read_to_end(&mut out).is_err());
}

#[test]
fn usage_counters_match_a_full_recount_and_quotas_hold() {
    let dir = tempfile::tempdir().unwrap();
    let e = Engine::open(dir.path(), false).unwrap();
    e.create_bucket("b").unwrap();
    let c = Condition::default();
    put(&e, "b", "a", b"12345", &c).unwrap();
    put(&e, "b", "a", b"123", &c).unwrap(); // overwrite, unversioned
    put(&e, "b", "x", b"x", &c).unwrap();
    e.delete_object("b", "x", None).unwrap();
    assert_eq!(e.usage("b").unwrap(), (3, 1));
    e.set_versioning("b", "Enabled").unwrap();
    put(&e, "b", "a", b"1234567", &c).unwrap(); // the null version is kept
    let marker = e.delete_object("b", "a", None).unwrap();
    assert_eq!(e.usage("b").unwrap(), (10, 2));
    e.delete_version("b", "a", marker.version_id.as_deref().unwrap(), false).unwrap();
    e.set_versioning("b", "Suspended").unwrap();
    put(&e, "b", "a", b"zz", &c).unwrap(); // replaces the null version
                                           // A multipart upload: parts count while in progress, then become the object.
    let u = e.create_upload(Upload { bucket: "b".into(), key: "m".into(), ..Default::default() }).unwrap();
    for n in 1..=2u32 {
        let mut w = e.new_blob().unwrap();
        w.write(&vec![b'p'; if n == 1 { MIN_PART_SIZE as usize } else { 10 }]).unwrap();
        let md5 = w.md5();
        let seg = w.seal().unwrap();
        e.put_part("b", "m", &u.id, Part { number: n, blob: seg.blob, size: seg.size, etag: hex::encode(&md5), md5, ..Default::default() })
            .unwrap();
    }
    let live = e.usage("b").unwrap();
    assert_eq!(live, (7 + 2 + MIN_PART_SIZE + 10, 2));
    let want: Vec<CompletePart> = (1..=2).map(|n| CompletePart { number: n, etag: String::new() }).collect();
    e.complete_upload("b", "m", &u.id, &want, &c, |_, _| Ok((BTreeMap::new(), String::new()))).unwrap();
    let live = e.usage("b").unwrap();
    assert_eq!(live, (7 + 2 + MIN_PART_SIZE + 10, 3));
    e.recover().unwrap();
    assert_eq!(e.usage("b").unwrap(), live, "recount after recovery");

    // Quota: growth past the limit is refused; deletes and shrinking overwrites still work.
    let q = serde_json::to_vec(&Quota { max_bytes: Some(live.0 + 5), max_objects: None }).unwrap();
    e.put_config("b", "quota", Some(&q)).unwrap();
    assert!(matches!(put(&e, "b", "big", &[0u8; 6], &c), Err(Error::QuotaExceeded)));
    put(&e, "b", "small", &[0u8; 5], &c).unwrap();
    assert!(matches!(put(&e, "b", "one-more", b"1", &c), Err(Error::QuotaExceeded)));
    put(&e, "b", "small", &[0u8; 2], &c).unwrap(); // shrinking is fine (suspended: replaces the null version)
    e.delete_object("b", "small", None).unwrap();
    let q = serde_json::to_vec(&Quota { max_bytes: None, max_objects: Some(3) }).unwrap();
    e.put_config("b", "quota", Some(&q)).unwrap();
    assert!(matches!(put(&e, "b", "n", b"1", &c), Err(Error::QuotaExceeded)));
    let rep = e.recover().unwrap();
    assert_eq!((rep.unreferenced_blobs_removed, rep.objects_with_missing_blobs), (0, 0));
}
