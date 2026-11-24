# Sport Channels — جدول المباريات والقنوات الناقلة

يولّد `out/matches.json`: مباريات اليوم (بتوقيت بغداد) ولكل مباراة كل القنوات الفضائية الناقلة
مع القمر والتردد والاستقطاب ومعدل الترميز والتشفير، والقنوات المجانية أولاً.

## التجربة على الكمبيوتر
```
pip install -r requirements.txt
python sport_channels.py --limit 3 --debug
```
- النتيجة: `out/matches.json`
- قنوات بلا تردد: `out/unmatched.txt` ← أضف لها سطراً في `aliases` داخل `config.json`
  (اسمها في LiveSoccerTV ← اسمها في KingOfSat)
- إذا فشل شيء: أرسل مجلد `debug/`

## التشغيل التلقائي (بدون كمبيوتر)
ارفع المجلد لمستودع GitHub، وانقل الملف `github-workflow-update.yml` إلى `.github/workflows/update.yml`، فيشتغل كل 3 ساعات.
رابط الملف للتطبيق:
`https://raw.githubusercontent.com/<USER>/<REPO>/main/out/matches.json`

## الإعدادات (config.json)
- `competitions_include` / `competitions_exclude_words`: البطولات
- `online_only_patterns`: خدمات إنترنت بلا قمر (تُستبعد من البحث)
- `aliases`: تصحيح أسماء القنوات
- `satellites_ar`, `satellite_priority`: أسماء الأقمار بالعربي وترتيبها

## أول تشغيل بطيء
أول مرة يبحث عن تردد كل قناة (قد يأخذ 20–40 دقيقة)، ويحفظها في `cache/channels_db.json`.
بعدها التشغيل سريع لأن الترددات محفوظة 7 أيام.
