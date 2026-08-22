#!/usr/bin/env python3
# ShapedDevices.csv names -> "<account#> <name> [<vendor>]".
# TIER1 dedicated-access always wins; then ZTEGC login->ZTE; then Mikrotik (ambiguous) over routers;
# then exact login match; then any inventory. Idempotent, 0644, skips inactive-* rows.
import sys, csv, re, os, tempfile
sys.path.insert(0,"/opt/libreqos-mcp"); import libreqos_mcp as lq
SD_IN  = sys.argv[1] if len(sys.argv)>1 else "/opt/libreqos/src/ShapedDevices.csv"
SD_OUT = sys.argv[2] if len(sys.argv)>2 else SD_IN
TIER1=["Tarana","Cambium","Ubiquiti","ZTE","Positron","Mimosa","Siklu","SAF","SIAE","Motorola"]  # dedicated access radios/ONUs
def t1pick(vs): return min(vs, key=lambda v: TIER1.index(v))
cust_name={c["id"]:(c.get("name") or "") for c in (lq._splynx_get("admin/customers/customer?limit=20000") or []) if isinstance(c,dict) and c.get("id") is not None}
items=lq._splynx_get("admin/inventory/items?limit=20000") or []
prods={p["id"]:p for p in (lq._splynx_get("admin/inventory/products?limit=20000") or []) if isinstance(p,dict)}
# Splynx 6 does NOT expose the vendor-id->name table via API 2.0: admin/inventory/vendors
# returns 404 (the endpoint is gated/removed for the API key; product.vendor_id is otherwise
# unresolvable through the API, and admin/inventory/suppliers is a DIFFERENT id-space). This is
# a static snapshot of Splynx's internal `monitoring_producers` dictionary, keyed by
# product.vendor_id -> the exact vendor name the Splynx UI shows. Refresh from the Splynx DB
# table `monitoring_producers` if new hardware vendors are added. (Ref: Splynx 5.2->6, 2026-08-14.)
PRODUCERS={
 1:"MikroTik",  2:"Cisco",     3:"Ericsson",  4:"D-Link",   5:"Juniper",
 6:"Ubiquiti",  7:"TP-Link",   8:"Other",     9:"Netonix", 10:"Siklu",
 11:"SIAE",     12:"SAF",      13:"Mimosa",   14:"Cambium", 15:"VZ",
 16:"ReadyNet", 17:"Voip Innovations",        18:"Planet Technologies",
 19:"ZTE",      20:"Proxmox",  21:"Zoom",     22:"Motorola", 23:"Tarana",
 24:"Vilo",     25:"TPLink",   26:"Nethatchet", 27:"Grandstream",
 28:"Positron", 29:"FWA",      30:"VZW",      31:"Android Phone", 32:"Archer",
}
vends=dict(PRODUCERS)
def iv(it):
    p=prods.get(it.get("product_id")); return vends.get(p.get("vendor_id")) if p else None
mac2v={}; ser2v={}; custitems={}
for it in items:
    if not isinstance(it,dict): continue
    bc=str(it.get("barcode") or "").upper(); sr=str(it.get("serial_number") or "").upper()
    if bc: mac2v[bc]=iv(it)
    if sr: ser2v[sr]=iv(it)
    custitems.setdefault(it.get("customer_id"),[]).append(it)
svc={}
for st in ("active","stopped","disabled","blocked"):
    for s in (lq._splynx_get(f"admin/customers/customer/0/internet-services?main_attributes%5Bstatus%5D={st}") or []):
        if isinstance(s,dict): svc[str(s.get("id"))]=(str(s.get("login") or "").upper(), s.get("customer_id"))
def vendor_for(login,cid):
    its=custitems.get(cid,[])
    t1=[it for it in its if iv(it) in TIER1]
    if t1:
        for it in t1:
            if login and login in (str(it.get("barcode") or "").upper(), str(it.get("serial_number") or "").upper()): return iv(it)
        return t1pick([iv(it) for it in t1])
    if login.startswith("ZTEGC"): return "ZTE"
    if any(iv(it)=="MikroTik" for it in its): return "MikroTik"
    v=mac2v.get(login) or ser2v.get(login)
    if v: return v
    ivs=[iv(it) for it in its if iv(it)]
    return ivs[0] if ivs else None
with open(SD_IN,newline="") as f: rows=list(csv.reader(f))
changed=0; total=0
for i,r in enumerate(rows):
    if i==0 or len(r)<12: continue
    total+=1
    if r[0].startswith("inactive-"): continue
    m=re.search(r"(\d+)$", r[0])
    if not m: continue
    login,custid=svc.get(m.group(1),("",None))
    if custid is None: continue
    cname=cust_name.get(custid, r[1]); v=vendor_for(login,custid)
    newname=f"{custid} {cname}"+(f" [{v}]" if v else "")
    if r[1]!=newname:
        r[1]=newname
        if len(r)>3: r[3]=newname
        changed+=1
d=os.path.dirname(os.path.abspath(SD_OUT))
fd,tmp=tempfile.mkstemp(dir=d,prefix=".sd_",suffix=".tmp")
with os.fdopen(fd,"w",newline="") as f: csv.writer(f,quoting=csv.QUOTE_ALL).writerows(rows)
os.chmod(tmp,0o644); os.replace(tmp,SD_OUT)
print(f"rename_devices: {changed}/{total} rows changed")
