# SOPHiA VCF Downloader

Bu otomasyon, SOPHiA CLI wrapper kullanarak hesaplardan `full_variant_table.vcf` dosyalarını indirir ve her run için sample metadata Excel dosyası oluşturur.

İş akışı:

1. Excel dosyasından indirilecek hesapları okur.
2. Her hesap için SOPHiA hesabına giriş yapar.
3. `status --limit` ile run ID listesini alır.
4. Her run için sample metadata bilgisini kaydeder.
5. Run dosyalarını listeler.
6. Sadece `full_variant_table.vcf` dosyalarını indirir.
7. İndirilen dosyaları kurum ve run bazlı klasörlere yerleştirir.
8. İşlem yarıda kesilirse kaldığı yerden devam edebilir.

## Input

Multi-client modda input bir Excel dosyasıdır.

Varsayılan kolonlar:

| Kolon | İçerik |
|---|---|
| 1 | `client_id` |
| 2 | kurum / hesap adı |
| 4 | `checked` bilgisi |

Sadece 4. kolonda tam olarak `checked` yazan satırlar işlenir.

Örnek:

```text
client_id | institution_name | ... | checked
203441    | Example Hospital  | ... | checked
20099     | Another Hospital  | ... | checked
21038     | Skipped Hospital  | ... |
```

Single-client modda Excel gerekmez. Script mevcut SOPHiA oturumundaki hesabı kullanır.

## Output

Varsayılan olarak çıktılar script'i çalıştırdığınız klasörün altında oluşur.

Multi-client çıktı yapısı:

```text
.
|-- accounts_manifest.tsv
|-- Institution Name 1
|   |-- run_ids.tsv
|   |-- download_manifest.tsv
|   |-- <RUN-ID-1>
|   |   |-- SAMPLE1_full_variant_table.vcf
|   |   `-- sample_metadata.xlsx
|   `-- <RUN-ID-2>
|       |-- SAMPLE2_full_variant_table.vcf
|       `-- sample_metadata.xlsx
`-- Institution Name 2
    |-- run_ids.tsv
    |-- download_manifest.tsv
    `-- ...
```

Single-client çıktı yapısı:

```text
.
`-- 203441
    |-- run_ids.tsv
    |-- download_manifest.tsv
    `-- <run_id>
        |-- <userRef>_full_variant_table.vcf
        `-- sample_metadata.xlsx
```


## Oluşan Dosyalar

| Dosya adı | Açıklama | Kolonlar |
|---|---|---|
| `run_ids.tsv` | Her kurum için alınan run listesini tutar. | `run_id`, `status` |
| `download_manifest.tsv` | Her kurum için run ve download durumunu tutar. | `run_id`, `file_id`, `userRef`, `analysisId`, `output_path`, `status`, `metadata_status`, `metadata_path`, `metadata_error`, `error` |
| `sample_metadata.xlsx` | Her run klasöründe oluşur. `Date`, `processDate` değerinin `DD-MM-YYYY` formatına çevrilmiş halidir. | `userRef`, `analysisType`, `genePanel`, `processDate`, `Date` |
| `accounts_manifest.tsv` | Multi-client çalışmada hesap bazlı genel sonucu tutar. | `client_id`, `institution_name`, `account_status`, `run_count`, `processed_run_count`, `downloaded_count`, `metadata_written_count`, `error` |

## Kullanılan SOPHiA CLI Komutları

Script, arka planda şu SOPHiA wrapper komutlarını çalıştırır:

```bash
python3 sg-upload-v2-wrapper.py login-iam --client-id <CLIENT_ID>
```

Multi-client modda her hesap için aktif hesaba giriş yapmak için kullanılır.

```bash
python3 sg-upload-v2-wrapper.py status --limit <N>
```

Run ID listesini almak için kullanılır.

```bash
python3 sg-upload-v2-wrapper.py sample --run-id <RUN_ID>
```

Run içindeki sample metadata bilgisini almak için kullanılır.

```bash
python3 sg-upload-v2-wrapper.py file --list --run-id <RUN_ID>
```

Run içindeki dosyaları listelemek için kullanılır.

```bash
python3 sg-upload-v2-wrapper.py file --download --file-id <FILE_ID> --file-out <OUTPUT_PATH>
```

Seçilen VCF dosyasını indirmek için kullanılır.

## VCF Seçimi ve İsimlendirme

Script, run içindeki dosyalar arasından sadece `full_variant_table.vcf` dosyalarını indirir. Seçim yaparken önce `filename` alanına bakar; bu alan `full_variant_table.vcf` değilse fallback olarak `name` alanını kontrol eder.

İndirilen dosyalar `userRef` bilgisiyle adlandırılır: `<userRef>_full_variant_table.vcf`. Böylece VCF dosyasının hangi sample'a ait olduğu dosya adından anlaşılır.

Eğer ilgili kayıtta `userRef` yoksa dosya kaybolmasın diye `unknown_<RUN_ID>_full_variant_table.vcf` adı kullanılır. Aynı run içinde aynı dosya adı birden fazla kez oluşursa, çakışmayı önlemek için dosya adına `analysisId` veya `fileId` eklenir.

## Requirements

Gerekli olanlar:

- Linux veya WSL
- Python 3.10+
- SOPHiA wrapper dosyası: `sg-upload-v2-wrapper.py`
- SOPHiA CLI için geçerli oturum / erişim
- Multi-client kullanım için `.xlsx` hesap dosyası

Ek bir pip paketi gerekmez. Script Excel okuma/yazma işlemlerini Python standart kütüphanesiyle yapar.

## Çalıştırma

Genel kullanım örneği:

```bash 
cd /path/to/output/folder

python3 /path/to/python/file/sophia_vcf_downloader.py \
  --accounts-xlsx /path/to/accounts/file/sophia_accounts.xlsx \
  --workers 4 \
  --download-timeout 900 \
  --download-start-timeout 180 \
  --download-stall-timeout 300

  ```

Kesilen bir işleme devam etmek için:

```bash
cd /path/to/output/folder

python3 /path/to/python/file/sophia_vcf_downloader.py \
  --accounts-xlsx /path/to/accounts/file/sophia_accounts.xlsx \
  --workers 4 \
  --resume-from-latest-manifest \
  --resume-verify-last-runs 10 \
  --download-timeout 900 \
  --download-start-timeout 180 \
  --download-stall-timeout 300

```

Script ve SOPHiA wrapper aynı klasördeyse:

```bash
python3 download_sophia_vcfss.py --accounts-xlsx ./sophia_accounts.xlsx
```

Worker sayısını belirlemek için:

```bash
python3 download_sophia_vcfss.py --accounts-xlsx ./sophia_accounts.xlsx --workers 4
```


Single-client çalıştırmak için:

```bash
python3 download_sophia_vcfss.py --client-folder 203441
```

Single-client modda script `login-iam` çalıştırmaz. Mevcut aktif SOPHiA hesabı üzerinden ilerler.

## Flagler

`--accounts-xlsx`: Multi-client hesap Excel dosyasının yolu.

```bash
--accounts-xlsx path/to/sophia_accounts.xlsx
```

`--workers`: Aynı anda kaç run işleneceğini belirler. Varsayılan değer `4`.

```bash
--workers 4
```

`--limit`: `status --limit` için kullanılacak run sayısı. Varsayılan değer `10000`.

```bash
--limit 10000
```

`--output-root`: Çıktıların yazılacağı ana klasör. Varsayılan değer mevcut klasör `./downloads`

```bash
--output-root main/path/to/output
```

`--client-id-col`, `--name-col`, `--checked-col`: Excel kolonlarını değiştirmek için kullanılır. Kolon numaraları 1'den başlar.

```bash
--client-id-col 1 --name-col 2 --checked-col 4
```


`--force`: Mevcut VCF dosyaları doğru boyutta görünse bile yeniden indirir.


`--resume-from-latest-manifest`: Multi-client modda en son güncellenmiş `download_manifest.tsv` hangi kurumdaysa oradan devam eder. Daha önce bitmiş kurumları tekrar taramaz.


`--resume-verify-last-runs`: Resume sırasında aktif kurumun manifestindeki son kaç run'ın yeniden kontrol edileceğini belirler. Varsayılan değer `10`.

```bash
--resume-verify-last-runs 10
```

`--no-resume`: Manifest'e göre atlama yapmaz. Run'ları tekrar işler.


`--command-timeout`: `login-iam`, `status`, `sample`, `file --list` komutları için timeout süresi. Varsayılan değer `300` saniye.

```bash
--command-timeout 300
```

`--download-timeout`: Tek bir VCF download komutu için toplam timeout süresi. Varsayılan değer `2700` saniye.

```bash
--download-timeout 2700
```

`--download-start-timeout`: Download başladıktan sonra `.vcf.part` dosyası bu süre içinde oluşmazsa download başarısız kabul edilir. Varsayılan değer `180` saniye.

```bash
--download-start-timeout 180
```

`--download-stall-timeout`: `.vcf.part` dosyasının boyutu bu süre boyunca artmazsa download takılmış kabul edilir. Varsayılan değer `300` saniye.

```bash
--download-stall-timeout 300
```

`--wrapper`: SOPHiA wrapper dosyasının yolu. Varsayılan değer `./sg-upload-v2-wrapper.py`.

```bash
--wrapper path/to/sg-upload-v2-wrapper.py
```


## Resume Yaklaşımı

Script resume destekler. Yani terminal kapanırsa, bağlantı koparsa veya işlem elle durdurulursa tekrar başlatıldığında daha önce tamamlanan dosyaları tekrar indirmemeye çalışır.

VCF için tamamlanma kontrolü:

1. Final `.vcf` dosyası var mı?
2. Dosya boyutu sıfırdan büyük mü?
3. SOPHiA `file --list` çıktısındaki `contentLength` ile lokal dosya boyutu eşleşiyor mu?

Bu kontroller geçerse download atlanır.

Geçici download dosyaları `.vcf.part` uzantısıyla yazılır. Download başarılı olursa `.part` dosyası final `.vcf` adına taşınır. Böylece yarım inmiş dosya final VCF gibi görünmez.

Metadata için tamamlanma kontrolü:

```text
sample_metadata.xlsx
```

dosyası varsa metadata adımı atlanır. Yeniden oluşturmak için `--force-metadata` kullanılabilir.

## Kesilen İşe Devam Etme

Multi-client çalışmada önerilen resume komutu:

```bash
python3 download_sophia_vcfss.py \
  --accounts-xlsx ./sophia_accounts.xlsx \
  --resume-from-latest-manifest \
  --resume-verify-last-runs 10
```

Bu komut:

1. En son güncellenmiş `download_manifest.tsv` dosyasını bulur.
2. O manifest hangi kuruma aitse o kurumdan devam eder.
3. Daha önceki kurumları tekrar taramaz.
4. Aktif kurumda son 10 run'ı yeniden kontrol eder.
5. Eksik, yarım veya boyutu uyuşmayan VCF varsa tekrar indirir.

Eğer her şeyi baştan kontrol etmek isterseniz `--resume-from-latest-manifest` kullanmayın.

## Terminalde Takip

Script terminalde hesap ve run ilerlemesini gösterir.

Örnek çıktı:

```text
[4/140] Example Hospital (client-id 12345): logging in
[4/140] Example Hospital (client-id 12345): fetching latest 10000 runs
[Example Hospital] Resume: skipping 120 runs, processing 35 active runs
[Example Hospital] Processed 25/35 active runs
[4/140] Example Hospital (client-id 12345): done downloaded=20, skipped_exists=15
```



