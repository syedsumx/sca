//! XML for S3: a small writer for responses and a minimal reader for the few request documents S3
//! clients send (CompleteMultipartUpload, Delete, CreateBucketConfiguration).

pub const XMLNS: &str = "http://s3.amazonaws.com/doc/2006-03-01/";

pub fn escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&apos;"),
            c if (c as u32) < 0x20 && c != '\t' && c != '\n' && c != '\r' => out.push_str(&format!("&#x{:X};", c as u32)),
            c => out.push(c),
        }
    }
    out
}

/// Writes an XML document element by element.
pub struct Writer {
    buf: String,
    stack: Vec<&'static str>,
}

impl Writer {
    pub fn new(root: &'static str) -> Writer {
        let mut w = Writer { buf: String::from("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"), stack: vec![] };
        w.buf.push_str(&format!("<{root} xmlns=\"{XMLNS}\">"));
        w.stack.push(root);
        w
    }

    pub fn open(&mut self, name: &'static str) -> &mut Self {
        self.buf.push_str(&format!("<{name}>"));
        self.stack.push(name);
        self
    }

    pub fn close(&mut self) -> &mut Self {
        let name = self.stack.pop().expect("unbalanced close");
        self.buf.push_str(&format!("</{name}>"));
        self
    }

    pub fn el(&mut self, name: &str, text: impl AsRef<str>) -> &mut Self {
        self.buf.push_str(&format!("<{name}>{}</{name}>", escape(text.as_ref())));
        self
    }

    /// Writes the element only when the text is not empty.
    pub fn opt(&mut self, name: &str, text: impl AsRef<str>) -> &mut Self {
        if !text.as_ref().is_empty() {
            self.el(name, text);
        }
        self
    }

    pub fn raw(&mut self, s: &str) -> &mut Self {
        self.buf.push_str(s);
        self
    }

    pub fn finish(mut self) -> String {
        while !self.stack.is_empty() {
            self.close();
        }
        self.buf
    }
}

/// A parsed element: name (without namespace prefix), text, children.
#[derive(Debug, Default)]
pub struct Element {
    pub name: String,
    pub text: String,
    pub children: Vec<Element>,
}

impl Element {
    pub fn child(&self, name: &str) -> Option<&Element> {
        self.children.iter().find(|c| c.name == name)
    }

    pub fn all<'a>(&'a self, name: &'a str) -> impl Iterator<Item = &'a Element> + 'a {
        self.children.iter().filter(move |c| c.name == name)
    }

    pub fn text_of(&self, name: &str) -> String {
        self.child(name).map(|c| c.text.trim().to_string()).unwrap_or_default()
    }

    /// The child's text exactly (object keys may begin or end with spaces).
    pub fn text_of_raw(&self, name: &str) -> String {
        self.child(name).map(|c| c.text.clone()).unwrap_or_default()
    }
}

fn unescape(s: &str) -> Option<String> {
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(i) = rest.find('&') {
        out.push_str(&rest[..i]);
        let end = rest[i..].find(';')? + i;
        let ent = &rest[i + 1..end];
        match ent {
            "amp" => out.push('&'),
            "lt" => out.push('<'),
            "gt" => out.push('>'),
            "quot" => out.push('"'),
            "apos" => out.push('\''),
            _ if ent.starts_with("#x") || ent.starts_with("#X") => out.push(char::from_u32(u32::from_str_radix(&ent[2..], 16).ok()?)?),
            _ if ent.starts_with('#') => out.push(char::from_u32(ent[1..].parse().ok()?)?),
            _ => return None,
        }
        rest = &rest[end + 1..];
    }
    out.push_str(rest);
    Some(out)
}

fn local(name: &str) -> String {
    name.rsplit(':').next().unwrap_or(name).to_string()
}

/// Parses a document into its root element. Returns None for malformed XML.
pub fn parse(doc: &str) -> Option<Element> {
    let mut stack: Vec<Element> = vec![];
    let mut root = None;
    let mut rest = doc;
    while !rest.is_empty() {
        if let Some(stripped) = rest.strip_prefix("<?") {
            rest = &stripped[stripped.find("?>")? + 2..];
        } else if let Some(stripped) = rest.strip_prefix("<!--") {
            rest = &stripped[stripped.find("-->")? + 3..];
        } else if let Some(stripped) = rest.strip_prefix("<![CDATA[") {
            let end = stripped.find("]]>")?;
            stack.last_mut()?.text.push_str(&stripped[..end]);
            rest = &stripped[end + 3..];
        } else if rest.starts_with("<!") {
            rest = &rest[rest.find('>')? + 1..];
        } else if let Some(stripped) = rest.strip_prefix("</") {
            let end = stripped.find('>')?;
            let name = local(stripped[..end].trim());
            let el = stack.pop()?;
            if el.name != name {
                return None;
            }
            match stack.last_mut() {
                Some(parent) => parent.children.push(el),
                None => root = Some(el),
            }
            rest = &stripped[end + 1..];
        } else if let Some(stripped) = rest.strip_prefix('<') {
            let end = stripped.find('>')?;
            let tag = &stripped[..end];
            let self_closing = tag.ends_with('/');
            let tag = tag.trim_end_matches('/');
            let name = local(tag.split_whitespace().next()?);
            let el = Element { name, ..Default::default() };
            if self_closing {
                match stack.last_mut() {
                    Some(parent) => parent.children.push(el),
                    None => root = Some(el),
                }
            } else {
                stack.push(el);
            }
            rest = &stripped[end + 1..];
        } else {
            let end = rest.find('<').unwrap_or(rest.len());
            let text = unescape(&rest[..end])?;
            match stack.last_mut() {
                Some(el) => el.text.push_str(&text),
                None if text.trim().is_empty() => {}
                None => return None,
            }
            rest = &rest[end..];
        }
        if root.is_some() && !rest.trim().is_empty() && !rest.trim_start().starts_with("<!--") {
            return None;
        }
    }
    if !stack.is_empty() {
        return None;
    }
    root
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_delete_document() {
        let doc = r#"<?xml version="1.0" encoding="UTF-8"?>
<Delete xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Quiet>true</Quiet>
  <Object><Key>a &amp; b</Key></Object><Object><Key>&lt;c&gt;&#x41;</Key><VersionId/></Object></Delete>"#;
        let root = parse(doc).unwrap();
        assert_eq!(root.name, "Delete");
        assert_eq!(root.text_of("Quiet"), "true");
        let keys: Vec<String> = root.all("Object").map(|o| o.text_of("Key")).collect();
        assert_eq!(keys, ["a & b", "<c>A"]);
        assert!(parse("<a><b></a>").is_none());
        assert!(parse("<a>x</a>trailing").is_none());
    }

    #[test]
    fn writer_escapes() {
        let mut w = Writer::new("ListBucketResult");
        w.el("Key", "a<b&c");
        assert!(w.finish().contains("<Key>a&lt;b&amp;c</Key></ListBucketResult>"));
    }
}
