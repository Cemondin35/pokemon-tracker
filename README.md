# 🎮 Pokemon Stock Tracker

17 İrlanda oyuncak sitesini takip eder.
Yeni ürün veya yeniden stoğa giren ürünler için **anında Telegram bildirimi** gönderir.

---

## ⚠️ ÖNEMLİ: Telegram Token Güvenliği

Token'ını herhangi bir yerde (chat, email vb.) paylaştıysan **hemen iptal et:**
1. @BotFather'a yaz → `/mybots` → botunu seç → `API Token` → `Revoke current token`
2. Yeni token'ı al ve `config.json`'a yaz

---

## 🚀 Kurulum (Local)

```bash
pip install -r requirements.txt
```

**Chat ID öğrenmek için:** Bota bir mesaj gönder, sonra şu URL'yi aç:
```
https://api.telegram.org/bot<TOKEN>/getUpdates
```
`"chat":{"id": 123456789}` — bu senin chat_id'n

**config.json'ı doldur:**
```json
{
  "telegram_token": "123456:ABC...",
  "telegram_chat_id": "123456789"
}
```

**Çalıştır:**
```bash
python tracker.py
```

---

## ☁️ Ücretsiz Sunucuda Çalıştırma (PC kapalıyken de çalışır)

### ✅ Railway.app — ÖNERİLEN

Ayda 500 saat ücretsiz. Bu tracker için yeterli (7x24 çalışmak için 720 saat lazım, bu yüzden aylık ~$5 Hobby planı alman gerekebilir — ama önce ücretsizle dene).

**Adımlar:**

1. **[railway.app](https://railway.app)** → GitHub ile kayıt ol

2. Bu klasörü GitHub'a yükle:
```bash
git init
git add .
git commit -m "pokemon tracker"
git branch -M main
# GitHub'da yeni bir repo oluştur, sonra:
git remote add origin https://github.com/KULLANICIN/REPO.git
git push -u origin main
```

3. Railway → **New Project** → **Deploy from GitHub repo** → repoyu seç

4. Deploy bittikten sonra **Variables** sekmesine git:
   - `TELEGRAM_TOKEN` = token'ın
   - `TELEGRAM_CHAT_ID` = chat id'n

5. Tracker başlar → Telegram'a "🚀 Başladı" mesajı gelir ✅

> Token'ı env variable olarak eklemek config.json'a yazmaktan daha güvenli.
> tracker.py zaten hem config.json hem env variable'ı kontrol eder.

---

### ✅ Oracle Cloud — Tamamen Ücretsiz VPS (7x24)

Oracle'ın **Always Free** tier'ı gerçekten ücretsiz ve sınırsız çalışır:

1. [cloud.oracle.com](https://cloud.oracle.com) → ücretsiz hesap aç
2. Compute → **Create Instance** → Always Free eligible seç (ARM veya AMD)
3. SSH ile bağlan, Python kur, scripti kopyala ve çalıştır:
```bash
# Sunucuya kopyala
scp -r pokemon_tracker/ ubuntu@SUNUCU_IP:~/

# Sunucuda çalıştır (arka planda)
cd pokemon_tracker
pip install -r requirements.txt
nohup python tracker.py &
```

---

## 📁 Dosyalar

| Dosya | Açıklama |
|-------|----------|
| `tracker.py` | Ana script |
| `config.json` | Site listesi + Telegram ayarları |
| `state.json` | Bilinen ürünler — otomatik oluşur |
| `tracker.log` | Log dosyası — otomatik oluşur |
| `Procfile` | Railway için başlatma komutu |
| `railway.json` | Railway konfigürasyonu |
| `requirements.txt` | Python kütüphaneleri |

---

## 🌐 Platform Tipleri

| Platform | Yöntem |
|----------|--------|
| `shopify` | Public JSON API — en güvenilir |
| `woocommerce` | HTML scraping |
| `prestashop` | HTML scraping |
| `bigcommerce` | HTML scraping |
| `generic` | HTML scraping (özel selector) |

---

## ➕ Yeni Shopify Site Eklemek

```json
{
  "name": "Yeni Site",
  "platform": "shopify",
  "base_url": "https://yenisite.ie",
  "collection_handle": "pokemon"
}
```

Sitenin Shopify olup olmadığını anlamak için URL'de `/collections/` varsa büyük ihtimalle Shopify'dır.
