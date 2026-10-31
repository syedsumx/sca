use std::io::Read;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use super::*;

fn put(e: &Engine, bucket: &str, key: &str, data: &[u8], cond: &Condition) -> Result<()> {
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
    e.reader(&o, 0, o.size).unwrap().read_to_end(&mut out).unwrap();
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
    e.reader(&obj, MIN_PART_SIZE - 2, 5).unwrap().read_to_end(&mut out).unwrap(); // a range spanning both parts
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
        let (obj, mut r) = match e.get_fresh("b", "k", |o| Ok::<_, ()>((0, o.size))) {
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
