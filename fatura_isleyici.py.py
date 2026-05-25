import xml.etree.ElementTree as ET
import pandas as pd
import streamlit as st
import io
import base64
import sqlite3
import random
import json
from lxml import etree
import ollama

# Sayfa Genişlik Ayarı
st.set_page_config(page_title="Ultra Fintech AI Robotu", page_icon="🤖", layout="wide")

# --- 1. VERİTABANI MOTORU ---
DB_NAME = "muhasebe_otomasyon.db"

def db_init():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS cari_mapping 
                      (vkn TEXT PRIMARY KEY, hesap_kodu TEXT, unvan TEXT, adres TEXT)''')
    cursor.execute('CREATE TABLE IF NOT EXISTS gider_mapping (kelime TEXT PRIMARY KEY, hesap_kodu TEXT)')
    
    # Kalıcı Yevmiye & Muavin Tablosu
    cursor.execute('''CREATE TABLE IF NOT EXISTS yevmiye_defteri (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        fatura_no TEXT UNIQUE,
                        tarih TEXT,
                        hesap_kodu TEXT,
                        unvan TEXT,
                        aciklama TEXT,
                        borc REAL,
                        alacak REAL,
                        fatura_html TEXT,
                        fis_detay_json TEXT
                      )''')
    
    # Tablo şema koruması
    cursor.execute("PRAGMA table_info(yevmiye_defteri)")
    mevcut_sutunlar = [column[1] for column in cursor.fetchall()]
    if "fatura_html" not in mevcut_sutunlar:
        cursor.execute("ALTER TABLE yevmiye_defteri ADD COLUMN fatura_html TEXT")
    if "fis_detay_json" not in mevcut_sutunlar:
        cursor.execute("ALTER TABLE yevmiye_defteri ADD COLUMN fis_detay_json TEXT")
        
    conn.commit()
    conn.close()

def db_cari_getir():
    conn = sqlite3.connect(DB_NAME)
    df = pd.read_sql_query("SELECT * FROM cari_mapping", conn)
    conn.close()
    return df

def db_cari_ekle(vkn, kod, unvan="", adres=""):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO cari_mapping (vkn, hesap_kodu, unvan, adres) VALUES (?, ?, ?, ?)", 
                   (str(vkn), str(kod), str(unvan), str(adres)))
    conn.commit()
    conn.close()

def db_her_seyi_temizle():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM cari_mapping")
    cursor.execute("DELETE FROM yevmiye_defteri")
    conn.commit()
    conn.close()

def db_fatura_kaydedilmis_mi(fatura_no):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM yevmiye_defteri WHERE fatura_no=? LIMIT 1", (fatura_no,))
    row = cursor.fetchone()
    conn.close()
    return row is not None

def db_yevmiye_kesin_kaydet(fatura_no, tarih, hesap_kodu, unvan, aciklama, borc, alacak, fatura_html="", fis_detay_list=None):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    fis_json = json.dumps(fis_detay_list, ensure_ascii=False) if fis_detay_list else ""
    try:
        cursor.execute("""INSERT OR REPLACE INTO yevmiye_defteri (fatura_no, tarih, hesap_kodu, unvan, aciklama, borc, alacak, fatura_html, fis_detay_json) 
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (fatura_no, tarih, hesap_kodu, unvan, aciklama, borc, alacak, fatura_html, fis_json))
        conn.commit()
    except Exception as e:
        pass
    finally:
        conn.close()

def db_muavin_dokum_getir(hesap_kodu):
    conn = sqlite3.connect(DB_NAME)
    query = "SELECT id, tarih as [Tarih], fatura_no as [Fatura No], aciklama as [Açıklama], borc as [Borç], alacak as [Alacak], fatura_html, fis_detay_json FROM yevmiye_defteri WHERE hesap_kodu=? ORDER BY tarih ASC, id ASC"
    df = pd.read_sql_query(query, conn, params=[hesap_kodu])
    conn.close()
    return df

def db_gider_getir():
    conn = sqlite3.connect(DB_NAME)
    df = pd.read_sql_query("SELECT * FROM gider_mapping", conn)
    conn.close()
    mapping = dict(zip(df['kelime'], df['hesap_kodu']))
    if "genel" not in mapping: mapping["genel"] = "770.01.001"
    return mapping

def db_gider_ekle(kelime, kod):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO gider_mapping (kelime, hesap_kodu) VALUES (?, ?)", (kelime.lower(), kod))
    conn.commit()
    conn.close()

db_init()

# --- 2. SAYAÇLI CANLI CANAVAR MOTORU ---
if "cari_hafizasi" not in st.session_state:
    df_ilk = db_cari_getir()
    st.session_state.cari_hafizasi = dict(zip(df_ilk['vkn'], df_ilk['hesap_kodu']))

if "file_uploader_key" not in st.session_state:
    st.session_state.file_uploader_key = 0

def canli_cari_kodu_uret(vkn, unvan, adres):
    vkn = str(vkn).strip()
    if vkn in st.session_state.cari_hafizasi:
        return st.session_state.cari_hafizasi[vkn]
        
    mevcut_kodlar = list(st.session_state.cari_hafizasi.values())
    filtreli_kodlar = [k for k in mevcut_kodlar if k.startswith("320.01.")]
    
    if filtreli_kodlar:
        try:
            sayilar = [int(k.split('.')[-1]) for k in filtreli_kodlar if k.split('.')[-1].isdigit()]
            yeni_sayi = max(sayilar) + 1
        except:
            yeni_sayi = len(filtreli_kodlar) + 1
    else:
        yeni_sayi = 1
        
    yeni_kod = f"320.01.{str(yeni_sayi).zfill(3)}"
    st.session_state.cari_hafizasi[vkn] = yeni_kod
    db_cari_ekle(vkn, yeni_kod, unvan, adres)
    return yeni_kod

# --- 3. XML PARSER MOTORU ---
class UltraEFaturaParser:
    def __init__(self, xml_icerik):
        self.xml_icerik = xml_icerik
        self.root = ET.fromstring(xml_icerik)
        self.ns = {
            'cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2',
            'cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2'
        }

    def fatura_ozetini_getir(self):
        fatura_no = self.root.find('cbc:ID', self.ns).text if self.root.find('cbc:ID', self.ns) is not None else f"F-{random.randint(1000,9999)}"
        tarih = self.root.find('cbc:IssueDate', self.ns).text if self.root.find('cbc:IssueDate', self.ns) is not None else "2026-01-01"
        doviz_el = self.root.find('cbc:DocumentCurrencyCode', self.ns)
        doviz = doviz_el.text if doviz_el is not None else "TRY"
        kur = 1.0
        kur_el = self.root.find('.//cac:TaxExchangeRate/cbc:CalculationRate', self.ns)
        if kur_el is not None: kur = float(kur_el.text)

        supplier = self.root.find('.//cac:AccountingSupplierParty/cac:Party', self.ns)
        vkn, unvan, tam_adres = None, None, "ADRES ALANINA ULAŞILAMADI"
        
        if supplier is not None:
            for id_el in supplier.findall('.//cbc:ID', self.ns):
                if id_el.text and id_el.text.isdigit() and len(id_el.text) in [10, 11]:
                    vkn = id_el.text
                    break
            
            unvan_adaylari = []
            unvan_el = supplier.find('.//cac:PartyName/cbc:Name', self.ns)
            if unvan_el is not None and unvan_el.text: unvan_adaylari.append(unvan_el.text.strip())
            legal_el = supplier.find('.//cac:PartyLegalEntity/cbc:RegistrationName', self.ns)
            if legal_el is not None and legal_el.text: unvan_adaylari.append(legal_el.text.strip())
            isim_el = supplier.find('.//cac:Person/cbc:FirstName', self.ns)
            soyisim_el = supplier.find('.//cac:Person/cbc:FamilyName', self.ns)
            if isim_el is not None and isim_el.text:
                ad_soyad = f"{isim_el.text.strip()} {soyisim_el.text.strip() if soyisim_el is not None and soyisim_el.text else ''}"
                unvan_adaylari.append(ad_soyad.strip())

            if unvan_adaylari: unvan = max(unvan_adaylari, key=len)

            adres_el = supplier.find('.//cac:PostalAddress', self.ns)
            if adres_el is not None:
                cadde = adres_el.find('cbc:StreetName', self.ns).text if adres_el.find('cbc:StreetName', self.ns) is not None else ""
                bina_no = adres_el.find('cbc:BuildingNumber', self.ns).text if adres_el.find('cbc:BuildingNumber', self.ns) is not None else ""
                ilce = adres_el.find('cbc:CitySubdivisionName', self.ns).text if adres_el.find('cbc:CitySubdivisionName', self.ns) is not None else ""
                sehir = adres_el.find('cbc:CityName', self.ns).text if adres_el.find('cbc:CityName', self.ns) is not None else ""
                tam_adres = f"{cadde} No:{bina_no} {ilce}/{sehir}".strip()
        
        if not vkn: vkn = f"V_{random.randint(100000, 999999)}"
        if not unvan: unvan = f"Bilinmeyen Tedarikçi (Fatura No: {fatura_no})"
            
        return {"FaturaNo": fatura_no, "Tarih": tarih, "Doviz": doviz, "Kur": kur, "CariVKN": vkn, "CariUnvan": unvan, "CariAdres": tam_adres}

    def satirlari_getir(self):
        satirlar = []
        for line in self.root.findall('.//cac:InvoiceLine', self.ns):
            urun_adi = line.find('.//cac:Item/cbc:Name', self.ns).text if line.find('.//cac:Item/cbc:Name', self.ns) is not None else "Hizmet/Mal"
            matrah = float(line.find('cbc:LineExtensionAmount', self.ns).text) if line.find('cbc:LineExtensionAmount', self.ns) is not None else 0.0
            kdv_el = line.find('.//cac:TaxSubtotal/cbc:Percent', self.ns)
            kdv_orani = float(kdv_el.text) if kdv_el is not None else 20.0
            kdv_tut_el = line.find('.//cac:TaxSubtotal/cbc:TaxAmount', self.ns)
            kdv_tutari = float(kdv_tut_el.text) if kdv_tut_el is not None else (matrah * kdv_orani / 100.0)
            
            iskonto_tutari = 0.0
            allowance = line.find('cac:AllowanceCharge', self.ns)
            if allowance is not None:
                charge_indicator = allowance.find('cbc:ChargeIndicator', self.ns)
                if charge_indicator is not None and charge_indicator.text == "false":
                    isk_el = allowance.find('cbc:Amount', self.ns)
                    if isk_el is not None: iskonto_tutari = float(isk_el.text)

            satirlar.append({
                "UrunAdi": urun_adi, "Matrah": matrah, "KDV_Orani": kdv_orani, 
                "KDV_Tutari": kdv_tutari, "Iskonto_Tutari": iskonto_tutari
            })
        return satirlar

    def html_goruntu_olustur(self):
        try:
            lxml_doc = etree.fromstring(self.xml_icerik)
            xslt_element = lxml_doc.xpath("//*[local-name()='embed-xslt'] | //*[local-name()='EmbeddedDocumentBinaryObject']")
            if xslt_element and xslt_element[0].text:
                try: xslt_text = base64.b64decode(xslt_element[0].text.strip()).decode('utf-8')
                except: xslt_text = xslt_element[0].text.strip()
                transform = etree.XSLT(etree.fromstring(xslt_text.encode('utf-8')))
                return str(transform(lxml_doc))
        except: pass
        return f"<div style='padding:20px; font-family:Arial;'>e-Fatura Görsel Şablonu Çözümlenmiştir.</div>"

class YerelYapayZekaMotoru:
    @st.cache_data(show_spinner="AI Muhasebe Kodunu Çözümlüyor...")
    def kod_tahmin_et(urun_adi):
        prompt = f"Türkiye Tek Düzen Hesap Planına göre '{urun_adi}' kalemi için sadece hesap kodu döndür (Örn: 770.01.001). Başka kelime yazma."
        try:
            cevap = ollama.generate(model='phi3', prompt=prompt)
            return cevap['response'].strip()[:10]
        except: return "770.01.001"

# --- 4. STREAMLIT SEKMELERİ ---
sayfa = st.sidebar.radio("Ekran Değiştir:", ["Fatura İşleme & Otomatik Muhasebe", "Cari Muavin Defteri (320)", "Kalıcı SQLite Veri Tabanı"])

if sayfa == "Kalıcı SQLite Veri Tabanı":
    st.title("⚙️ Kalıcı SQLite Veritabanı Yönetimi (Toplu Silme Dünyası)")
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("🏢 Cari & Muavin Veritabanı Havuzu")
        cari_df = db_cari_getir()
        
        if not cari_df.empty:
            cari_df["gosterim_adi"] = cari_df["hesap_kodu"] + " - " + cari_df["unvan"]
            secilen_firma_adi = st.selectbox("Aramak veya incelemek istediğiniz cariyi seçin:", cari_df["gosterim_adi"].tolist())
            secilen_satir = cari_df[cari_df["gosterim_adi"] == secilen_firma_adi].iloc[0]
            
            st.info(f"""
            **📋 Cari Bilgi Kartı**
            * **Hesap Kodu:** `{secilen_satir['hesap_kodu']}`
            * **Vergi No (VKN):** `{secilen_satir['vkn']}`
            * **Firma Ünvanı:** {secilen_satir['unvan']}
            * **Ayıklanan Kurumsal Adres:** _{secilen_satir['adres']}_
            """)
        else:
            st.warning("Veritabanında kayıtlı cari hesap bulunamadı.")
            
        st.divider()
        st.error("🚨 KRİTİK ALAN: VERİTABANI TOPLU SİLME SİSTEMİ")
        if st.button("🗑️ TÜM ARŞİVİ VE FATURALARI TOPLU SİL (SIFIRLA)", type="primary", use_container_width=True):
            db_her_seyi_temizle()
            st.session_state.cari_hafizasi = {}
            st.success("Tüm sistem hafızası, cariler ve muavin kayıtları başarıyla sıfırlandı!")
            st.rerun()
            
    with col2:
        st.subheader("📦 AI Gider Kuralları")
        gider_haritasi = db_gider_getir()
        st.dataframe(pd.DataFrame(list(gider_haritasi.items()), columns=["Anahtar Kelime", "Hesap Kodu"]), use_container_width=True)
        kelime = st.text_input("Kelime:")
        g_kod = st.text_input("Gider Kodu:")
        if st.button("Kuralı Kaydet"):
            if kelime and g_kod: db_gider_ekle(kelime, g_kod); st.success("Kaydedildi!"); st.rerun()

elif sayfa == "Cari Muavin Defteri (320)":
    st.title("📖 320 Cari Hesap Muavin Defteri")
    st.markdown("💡 **Canlı Takip:** _Aşağıdaki yevmiye tablosunda bir satıra tıkladığınız an, evrak resmi ve fiş detayı hemen altta canlanır._")
    st.divider()
    
    cari_df = db_cari_getir()
    if not cari_df.empty:
        cari_df["gosterim_adi"] = cari_df["hesap_kodu"] + " - " + cari_df["unvan"]
        secilen_muavin = st.selectbox("Defterini incelemek istediğiniz cari hesabı seçin:", cari_df["gosterim_adi"].tolist())
        secilen_kod = secilen_muavin.split(" - ")[0]
        
        muavin_df = db_muavin_dokum_getir(secilen_kod)
        
        if not muavin_df.empty:
            bakiye_serisi = []
            guncel_bakiye = 0.0
            for idx, row in muavin_df.iterrows():
                guncel_bakiye += (row["Alacak"] - row["Borç"])
                bakiye_serisi.append(round(guncel_bakiye, 2))
            muavin_df["Yürüyen Bakiye"] = bakiye_serisi
            
            c1, c2, c3 = st.columns(3)
            c1.metric("📉 Toplam Borç Ödemesi", f"{muavin_df['Borç'].sum():,.2f} TL")
            c2.metric("📈 Toplam Alacak / Fatura", f"{muavin_df['Alacak'].sum():,.2f} TL")
            c3.metric("💰 Güncel Kalan Bakiye", f"{(muavin_df['Alacak'].sum() - muavin_df['Borç'].sum()):,.2f} TL")
            
            st.divider()
            gosterilecek_muavin = muavin_df[["Tarih", "Fatura No", "Açıklama", "Borç", "Alacak", "Yürüyen Bakiye"]]
            
            secilen_satirlar = st.dataframe(
                gosterilecek_muavin, 
                use_container_width=True, 
                hide_index=True,
                on_select="rerun",
                selection_mode="single-row"
            )
            
            if secilen_satirlar and len(secilen_satirlar["selection"]["rows"]) > 0:
                secili_indeks = secilen_satirlar["selection"]["rows"][0]
                secilen_evrak_satiri = muavin_df.iloc[secili_indeks]
                
                st.divider()
                st.markdown(f"### 🔍 Seçilen Evrak Kayıt Detayı: {secilen_evrak_satiri['Fatura No']}")
                
                pop_sol, pop_sag = st.columns([1, 1])
                with pop_sol:
                    st.markdown("#### 📄 Fatura Orijinal Görseli")
                    if secilen_evrak_satiri["fatura_html"]:
                        st.components.v1.html(secilen_evrak_satiri["fatura_html"], height=550, scrolling=True)
                    else: st.warning("HTML bulunamadı.")
                with pop_sag:
                    st.markdown("#### 📊 Muhasebeleşme Fiş Detayı")
                    if secilen_evrak_satiri["fis_detay_json"]:
                        saved_fis_list = json.loads(secilen_evrak_satiri["fis_detay_json"])
                        st.dataframe(pd.DataFrame(saved_fis_list), use_container_width=True, hide_index=True)
                        st.success("🧾 Yevmiye Fiş Girişi Başarıyla Atılmış Durumda.")
        else: st.warning("Bu cari hesaba ait henüz kayıtlı hareket yok.")
    else: st.warning("Kayıtlı cari hesap kartı bulunamadı.")

else:
    st.title("🚀 Tam Otomatik Cari & AI Muhasebe Robotu")
    st.divider()

    yuklenen_dosyalar = st.file_uploader("Fatura XML dosyalarını bırakın:", type=["xml"], accept_multiple_files=True, key=f"uploader_{st.session_state.file_uploader_key}")

    if yuklenen_dosyalar:
        gider_haritasi = db_gider_getir()

        # Ön tarama ve canlı cari kodu üretme döngüsü
        yeni_eklenen_var_mi = False
        for d in yuklenen_dosyalar:
            d.seek(0); d_bytes = d.read(); d.seek(0)
            try:
                p_temp = UltraEFaturaParser(d_bytes)
                o_temp = p_temp.fatura_ozetini_getir()
                v_temp = o_temp["CariVKN"]
                
                if v_temp not in st.session_state.cari_hafizasi:
                    canli_cari_kodu_uret(v_temp, o_temp["CariUnvan"], o_temp["CariAdres"])
                    yeni_eklenen_var_mi = True
            except: pass

        if yeni_eklenen_var_mi:
            st.rerun()

        dosya_isimleri = [d.name for d in yuklenen_dosyalar]
        secilen_dosya_adi = st.selectbox("İncelemek istediğiniz faturayı seçin:", dosya_isimleri)
        secilen_dosya = next(d for d in yuklenen_dosyalar if d.name == secilen_dosya_adi)
        
        xml_bytes = secilen_dosya.read(); secilen_dosya.seek(0)
        parser = UltraEFaturaParser(xml_bytes)
        ozet = parser.fatura_ozetini_getir()
        satirlar = parser.satirlari_getir()
        html_fatura = parser.html_goruntu_olustur()

        vkn = ozet["CariVKN"]
        cari_hesap_kodu = st.session_state.cari_hafizasi.get(vkn, "320.01.999")

        sol, sag = st.columns([1, 1])

        with sol:
            st.subheader("📊 Akıllı Fiş Dağıtım Detayı")
            st.markdown(f"**Atanan Cari Kod:** `{cari_hesap_kodu}` | **Ünvan:** {ozet['CariUnvan']}")
            st.caption(f"**Adres:** {ozet['CariAdres']}")
            st.divider()
            
            fatura_fişi = []
            fatura_toplam_borc = 0.0

            for satir in satirlar:
                urun_adi_lower = satir["UrunAdi"].lower()
                g_kod = None
                for k, v in gider_haritasi.items():
                    if k in urun_adi_lower and k != "genel": g_kod = v; break
                
                if g_kod is None: g_kod = YerelYapayZekaMotoru.kod_tahmin_et(satir['UrunAdi'])

                tl_matrah = round(satir["Matrah"] * ozet["Kur"], 2)
                tl_kdv = round(satir["KDV_Tutari"] * ozet["Kur"], 2)
                k_kod = "191.01.020" if satir["KDV_Orani"] == 20 else ("191.01.010" if satir["KDV_Orani"] == 10 else "191.01.001")
                
                fatura_fişi.append({"Hesap Kodu": g_kod, "Açıklama": f"{satir['UrunAdi']} Matrahı", "Borç": tl_matrah, "Alacak": 0.0})
                fatura_toplam_borc += tl_matrah
                fatura_fişi.append({"Hesap Kodu": k_kod, "Açıklama": f"%{satir['KDV_Orani']} KDV", "Borç": tl_kdv, "Alacak": 0.0})
                fatura_toplam_borc += tl_kdv

            round_toplam = round(fatura_toplam_borc, 2)
            fatura_fişi.append({"Hesap Kodu": cari_hesap_kodu, "Açıklama": "Fatura Kapatma", "Borç": 0.0, "Alacak": round_toplam})
            st.dataframe(pd.DataFrame(fatura_fişi), use_container_width=True, hide_index=True)
            
            st.markdown("---")
            st.write("📂 **Kalıcı Muavin Entegrasyon Paneli:**")
            
            # --- YENİ EKLENEN TOPLU EKLE-SİL MOTORU ---
            col_b1, col_b2 = st.columns(2)
            with col_b1:
                fatura_zaten_kayitli = db_fatura_kaydedilmis_mi(ozet["FaturaNo"])
                if fatura_zaten_kayitli:
                    st.warning("⚠️ Bu fatura zaten işlenmiş.")
                else:
                    if st.button("💾 Sadece Bu Faturayı İşle", type="primary", use_container_width=True):
                        db_yevmiye_kesin_kaydet(ozet["FaturaNo"], ozet["Tarih"], cari_hesap_kodu, ozet["CariUnvan"], f"{ozet['FaturaNo']} e-Fatura Alımı", 0.0, round_toplam, html_fatura, fatura_fişi)
                        st.success("İşlendi!")
                        st.rerun()
            
            with col_b2:
                # 🔥 TOPLU EKLEME BUTONU
                if st.button("📥 YÜKLENEN TÜM FATURALARI TOPLU KAYDET", use_container_width=True):
                    basarili_sayac = 0
                    for d in yuklenen_dosyalar:
                        try:
                            d.seek(0); d_bytes = d.read(); d.seek(0)
                            p_all = UltraEFaturaParser(d_bytes)
                            o_all = p_all.fatura_ozetini_getir()
                            
                            if not db_fatura_kaydedilmis_mi(o_all["FaturaNo"]):
                                s_all = p_all.satirlari_getir()
                                v_all = o_all["CariVKN"]
                                c_k = canli_cari_kodu_uret(v_all, o_all["CariUnvan"], o_all["CariAdres"])
                                
                                t_borc = 0.0
                                local_fis = []
                                for s in s_all:
                                    g_k = None
                                    for k, v in gider_haritasi.items():
                                        if k in s["UrunAdi"].lower() and k != "genel": g_k = v; break
                                    if g_k is None: g_k = YerelYapayZekaMotoru.kod_tahmin_et(s["UrunAdi"])
                                    
                                    m_tl = round(s["Matrah"] * o_all["Kur"], 2)
                                    k_tl = round(s["KDV_Tutari"] * o_all["Kur"], 2)
                                    k_k = "191.01.020" if s["KDV_Orani"] == 20 else "191.01.001"
                                    
                                    local_fis.append({"Hesap Kodu": g_k, "Açıklama": s["UrunAdi"], "Borç": m_tl, "Alacak": 0.0})
                                    local_fis.append({"Hesap Kodu": k_k, "Açıklama": "KDV", "Borç": k_tl, "Alacak": 0.0})
                                    t_borc += (m_tl + k_tl)
                                
                                r_t = round(t_borc, 2)
                                local_fis.append({"Hesap Kodu": c_k, "Açıklama": "Fatura Kapatma", "Borç": 0.0, "Alacak": r_t})
                                
                                db_yevmiye_kesin_kaydet(o_all["FaturaNo"], o_all["Tarih"], c_k, o_all["CariUnvan"], f"{o_all['FaturaNo']} Toplu Fatura Kaydı", 0.0, r_t, p_all.html_goruntu_olustur(), local_fis)
                                basarili_sayac += 1
                        except: pass
                    st.success(f"🎉 Harika! Toplam {basarili_sayac} adet fatura tek tıkla muavin defterine işlendi!")
                    st.rerun()

            # --- LUCA / ZİRVE UYUMLU TOPLU EXCEL AKTARIMI ---
            tum_yevmiye_satirlari = []
            for d in yuklenen_dosyalar:
                try:
                    d.seek(0); d_bytes = d.read(); d.seek(0)
                    p_all = UltraEFaturaParser(d_bytes)
                    o_all = p_all.fatura_ozetini_getir()
                    s_all = p_all.satirlari_getir()
                    v_all = o_all["CariVKN"]
                    c_k = canli_cari_kodu_uret(v_all, o_all["CariUnvan"], o_all["CariAdres"])
                        
                    t_borc = 0.0
                    for s in s_all:
                        g_k = None
                        for k, v in gider_haritasi.items():
                            if k in s["UrunAdi"].lower() and k != "genel": g_k = v; break
                        if g_k is None: g_k = YerelYapayZekaMotoru.kod_tahmin_et(s["UrunAdi"])
                        
                        m_tl = round(s["Matrah"] * o_all["Kur"], 2)
                        k_tl = round(s["KDV_Tutari"] * o_all["Kur"], 2)
                        k_k = "191.01.020" if s["KDV_Orani"] == 20 else "191.01.001"
                        tum_yevmiye_satirlari.append({"Fiş No": o_all["FaturaNo"], "Hesap Kodu": g_k, "Borç": m_tl, "Alacak": 0.0, "Açıklama": s["UrunAdi"]})
                        tum_yevmiye_satirlari.append({"Fiş No": o_all["FaturaNo"], "Hesap Kodu": k_k, "Borç": k_tl, "Alacak": 0.0, "Açıklama": "KDV"})
                        t_borc += (m_tl + k_tl)
                    tum_yevmiye_satirlari.append({"Fiş No": o_all["FaturaNo"], "Hesap Kodu": c_k, "Borç": 0.0, "Alacak": round(t_borc, 2), "Açıklama": "Cari Kapatma"})
                except: pass

            if tum_yevmiye_satirlari:
                df_luca = pd.DataFrame(tum_yevmiye_satirlari)
                buf = io.BytesIO()
                with pd.ExcelWriter(buf, engine='openpyxl') as wr:
                    df_luca.to_excel(wr, index=False, sheet_name="Luca_Zirve")
                buf.seek(0)
                st.divider()
                st.download_button("📥 Luca / Zirve Uyumlu Fiş Aktarım Excelini İndir", buf, file_name="Muhasebe_Aktarim.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        with sag:
            st.subheader("📄 Canlı Fatura Resmi")
            st.components.v1.html(html_fatura, height=750, scrolling=True)
