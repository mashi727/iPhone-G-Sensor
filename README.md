# iPhone G-Sensor Logger & Viewer

iPhoneのモーションセンサー（加速度計、ジャイロスコープ、磁力計）とGPSを使用した高精度センサーログ記録・可視化システム

## 考え方

何が問題で、それをどう解いているかを PAD（問題分析図）で示します。各段の詳細は下の各節を参照してください。

<img src="docs/pad/concept.png" alt="考え方の PAD。移動中のセンサーを記録しあとで検証するため、iPhone でセンサーと GPS を同じログに記録し、GPS 途絶時は推測航法で補い、机上のビューアで要約・グラフ・地図により照合する" width="100%">

<sub>図の元は [`docs/pad/concept.spd`](docs/pad/concept.spd)。[padkit](https://github.com/mashi727/padkit) で検査・描画しています。</sub>

## Demo

### Sensor Logger (iPhone - Pythonista3)

https://github.com/user-attachments/assets/8bcaa9ff-bb84-4976-832b-16496f7f16e1

**機能:**
- 加速度・ジャイロ・姿勢・磁場をリアルタイム記録
- GPS位置・速度・精度をトラッキング
- OpenStreetMap上でリアルタイム位置表示
- デッドレコニング（GPS途絶時の推測航法）
- JSONフォーマットでログ出力

### Log Viewer (Desktop - PySide6)

https://github.com/user-attachments/assets/036c52fd-8439-4fb0-b0da-9384105489ef

**機能:**
- センサーデータの時系列グラフ表示（PyQtGraph）
- 地図上での軌跡表示（Leaflet.js。OpenStreetMap / 国土地理院の淡色・標準・写真を切り替え）
- GPS 精度で色分けした航跡と、デッドレコニング・GPS/INS 融合の比較表示
- 高度断面図（国内のログでは国土地理院の標高タイルによる地形断面を重ねる）
- GPS 有効率・記録欠落・DR 作動の要約（間引き前の全レコードで集計）
- 大容量ログのストリーミング読み込みと間引き表示
- ファイルブラウザによるログ選択

※ デモ動画は旧版のものです（地図は国土地理院の淡色地図）。

## Case Study: 旅客機の機内で記録したログ

フィンランドから英国マンチェスターへ向かう旅客機の機内で記録したログを Log Viewer で表示した例です。記録の開始地点はヘルシンキ・ヴァンター空港、終了地点はマンチェスター空港の構内です。地上を移動する前提で作ったツールが、時速 750 km・高度 11 km という想定外の条件でどこまで使えるか、また何が見えるかの検証を兼ねています。

| 項目 | 値 |
| --- | --- |
| 記録時間 | 186.9 分（11,214 s） |
| レコード数 | 102,525 件（平均 9.1 Hz）、JSON 195 MB |
| 読み込み | ストリーミングで 10,001 件に間引いて表示（約 4 秒） |
| GPS 有効率 | 99.98 %（水平精度の中央値 2.7 m、99 パーセンタイル 10.7 m） |
| 巡航 | GPS 高度の中央値 11,761 m、対地速度の中央値 209 m/s（754 km/h） |
| 飛行距離 | GPS 航跡で 1,872 km（出発地点と到着地点の大圏距離は 1,811 km） |

数値はすべてログの全レコードから算出しています（間引き前）。

### Integrated Track — 航跡と高度断面

![Integrated Track](assets/screenshots/log_viewer_flight_integrated_track.png)

- 1,872 km の航跡を GPS 精度で色分けして 1 枚の地図に描きます。機内でも GPS はほぼ途切れず、航跡の 99.8 % が GPS 区間でした。
- 下段の断面図は、横軸を距離 (km)、縦軸を GPS 高度 (m) に固定しています。飛行の段階を断面の形からそのまま読み取れます。
  - 離陸から約 16 分で高度 10,000 m に到達（平均上昇率 約 10 m/s）
  - 約 11.8 km の巡航を 109 分維持
  - 10,000 m から約 25 分かけて着陸
- 断面図のオレンジの区間をドラッグすると、その区間の両端が地図上にマーカーで示されます。高度の変化と地理的な位置を対応づけられます。
- 地形断面（国土地理院の標高タイル）は日本国内しか提供されていないため、国外のログでは取得しません。国内のログでも取得はバックグラウンドで行うので、表示を待たせません。

### GPS — 高度・対地速度・測位精度

![GPS](assets/screenshots/log_viewer_flight_gps.png)

- 高度・対地速度・水平精度を同じ時間軸に並べるので、飛行の各段階をグラフから区切れます。
  - 記録開始から 13 分後に地上走行を開始
  - 離陸滑走、上昇、巡航、降下、着陸後の地上走行
- 離陸滑走では、GPS 速度が 10 m/s から 70 m/s まで 26 秒で上がりました。平均加速度は 2.3 m/s²（0.24 G）です。
- 水平精度は機内でも中央値 2.7 m でした。一時的な劣化は降下中に集中しており（最大 約 100 m）、その区間を精度のグラフで特定できます。

### Acceleration — 重力方向とユーザー加速度

![Acceleration](assets/screenshots/log_viewer_flight_acceleration.png)

- 上段は、端末の座標系で見た重力の方向です。段差状の変化はジャイロのスパイクと同じ時刻に起きています。したがって機体の運動ではなく、端末を置き直した時刻と判断できます。
- 離陸時（t ≈ 1.1 ks）に重力の Y 成分が −0.08 G から −0.51 G まで振れ、上昇が進むにつれて −0.18 G へ戻ります。これは機体のピッチ姿勢と、持続する加速度の一部が重力の推定に混入した分が重なったものと解釈できます。ただし端末の置き方を記録していないため、両者は分離できません。
- 下段は重力を除いたユーザー加速度です。巡航中の大きさは中央値 0.021 G、99 パーセンタイル 0.093 G でした。
- GPS と IMU を同じログに持っていることで、両者を突き合わせて検証できます。たとえば離陸滑走中、IMU が示す前後方向の加速度は平均 0.14 G で、GPS 速度から求めた 0.24 G より小さくなっています。持続する加速度の一部が、姿勢推定の側で重力として扱われていることがわかります。
- 9 ks 以降は着陸前後の端末操作で大きく乱れています。斜めの直線は、記録が欠落した区間の前後を結んだものです。

### Attitude — 姿勢とジャイロ

![Attitude](assets/screenshots/log_viewer_flight_attitude.png)

- 姿勢角とジャイロを上下に並べています。姿勢角の段差がジャイロのスパイクと同じ時刻にあれば端末の操作、スパイクを伴わないゆっくりした変化であれば機体の運動、と切り分ける手がかりになります。
- 巡航中はジャイロがほぼ 0 rad/s で、端末を静置していた区間を特定できます。

### Magnetic — 機内の磁場

![Magnetic](assets/screenshots/log_viewer_flight_magnetic.png)

- 地球磁場の強さは北欧〜英国で約 50 µT です。これに対し、機内の測定値の大きさは中央値 79 µT、95 パーセンタイル 109 µT でした。値は端末を動かすたびに段差状に変わります。
- 機体の構造や電装品の影響と、端末自身の較正誤差とは区別できません。いずれにせよ、機内では磁気方位に頼る航法が成り立たないことがログから直接わかります。

### Dead Reckoning — GPS 途絶との照合

![Dead Reckoning](assets/screenshots/log_viewer_flight_dead_reckoning.png)

- 推測航法（DR）は 20 回作動しましたが、**実際に GPS が途絶して作動したものは 0 回**でした。20 回はすべて、記録が 3 秒以上欠落した後、記録が再開された最初のレコードで起きています。再開直後はロガーが保持している GPS の時刻が古いため、タイムアウト（5 秒）と判定されて DR が作動します。
- Viewer は間引く前の全レコードを走査して、次の 2 点を右側のパネルに表示します。記録の欠落区間は、速度グラフに背景色で示します。
  - GPS 有効率と、記録欠落の回数・合計時間（今回は 20 回、計 754 秒）
  - DR が作動した原因の内訳（GPS 途絶か、記録の再開か）
- 最終 GPS 測位点から推測位置までの隔たりは、最大 58 km でした。これは 243 秒の欠落の直後の値で、欠落中に機体が進んだ距離をそのまま反映しています。
- このログから、ロガー側の改善点が 2 つ見つかりました。どちらもロガー v1.2.0 で対応しています。
  - 記録の再開直後を GPS 途絶と誤って判定する。v1.2.0 では、更新が 2 秒以上止まった後の再開を検出し、次のように扱います。
    - GPS のタイムアウトは再開時点から数え直す
    - 新しい測位が届くまでは、停止前の位置を航法に使わない
    - 停止していた時間を積分に使わない（停止中の移動を一度に外挿しない）
    - 停止していた秒数をログの `resumed_after_sec` に記録する
  - DR の方位が 0–360° の範囲に正規化されていない（−94°〜396° の値が出る）
- 記録欠落の原因（アプリの一時停止など）は、ログからは特定できません。

## System Architecture

```mermaid
flowchart TB
    subgraph iPhone["📱 iPhone (Pythonista3)"]
        subgraph Sensors["Sensor APIs"]
            Motion["🎯 Motion API<br/>Accel / Gyro<br/>Attitude / Magnet"]
            Location["📍 Location API<br/>GPS / Speed<br/>Heading / Accuracy"]
        end
        DR["🧭 Dead Reckoning<br/>GPS Outage Fallback"]
        Fusion["🔀 GPS/INS Fusion<br/>Integrated Track"]
        Export["💾 JSON Log Export<br/>(10Hz sampling)"]

        Motion --> DR
        Location --> DR
        Motion --> Fusion
        Location --> Fusion
        Motion --> Export
        Location --> Export
        DR --> Export
        Fusion --> Export
    end

    Export -->|"File Transfer"| Import

    subgraph Desktop["🖥️ Desktop Viewer (PySide6)"]
        Import["📂 File Browser<br/>Streaming Load"]
        Summary["📋 Log Summary<br/>GPS Availability / Gaps / DR"]
        subgraph Visualization["Visualization"]
            Graph["📈 PyQtGraph<br/>Time Series / Elevation Profile"]
            Map["🗺️ Leaflet.js<br/>OSM / GSI Tiles"]
        end
        Import --> Summary
        Import --> Graph
        Import --> Map
    end
```

## Requirements

### Sensor Logger (iPhone)
- iPhone with motion sensors
- [Pythonista 3](http://omz-software.com/pythonista/) app

### Log Viewer (Desktop)
- Python 3.10+
- Dependencies listed in `requirements.txt`
  - PySide6（QtWebEngine を含む）、pyqtgraph、numpy
  - ijson：大容量ログ（20 MB 超）のストリーミング読み込み
  - Pillow：国土地理院の標高タイルの読み取り（地形断面）

## Installation

### Log Viewer Setup

```bash
# Clone repository
git clone https://github.com/mashi727/iPhone-G-Sensor.git
cd iPhone-G-Sensor

# Install dependencies
pip install -r requirements.txt

# Run viewer (引数で開くフォルダを指定。省略時はカレントフォルダ)
python log_viewer.py data/
```

### Sensor Logger Setup

1. Install Pythonista 3 on your iPhone
2. Copy `g_sensor_app.py` to Pythonista
3. Run the script

## Sensor Data Format

ログファイルは JSON 形式です。1 レコードが 1 回の更新（約 100 ms）に対応します。値は例です。

```json
{
  "metadata": {
    "session_start": "2026-01-01T12:00:00.000000",
    "app_version": "1.2.0",
    "update_interval_ms": 100,
    "device_info": {"model": "iPhone", "system_name": "iOS", "system_version": "26.x"},
    "sensors_available": {"altimeter": false, "sleep_control": true, "direct_gyro": false}
  },
  "record_count": 1,
  "records": [
    {
      "timestamp": 1767268800.0,
      "datetime": "2026-01-01T12:00:00.000000",
      "sequence": 0,
      "dt": 0.1,
      "resumed_after_sec": null,
      "sensors": {
        "gravity":           {"x": 0.0, "y": 0.0, "z": -1.0, "magnitude": 1.0},
        "user_acceleration": {"x": 0.01, "y": -0.02, "z": 0.0, "magnitude": 0.022},
        "raw_acceleration":  {"x": 0.01, "y": -0.02, "z": -1.0, "magnitude": 1.0},
        "attitude": {"roll_rad": 0.0, "pitch_rad": 0.0, "yaw_rad": 0.0,
                     "roll_deg": 0.0, "pitch_deg": 0.0, "yaw_deg": 0.0},
        "magnetic_field": {"x": 25.0, "y": -10.0, "z": 40.0, "magnitude": 48.2, "accuracy": 2},
        "gyro_calculated": {"x": 0.0, "y": 0.0, "z": 0.0},
        "barometer": null
      },
      "gps": {
        "raw": {
          "latitude": 35.6812, "longitude": 139.7671, "altitude": 40.0,
          "speed": 1.5, "speed_clamped": 1.5, "course": 90.0,
          "horizontal_accuracy": 5.0, "vertical_accuracy": 3.0, "timestamp": 1767268800.0
        },
        "status": "good",
        "no_signal": false,
        "stale": false
      },
      "dead_reckoning": {"active": false, "result": null},
      "gps_ins_fusion": {"latitude": 35.6812, "longitude": 139.7671, "speed": 1.5,
                         "heading": 90.0, "mode": "ins", "memory_elapsed": 0.0},
      "integrated_track": {"latitude": 35.6812, "longitude": 139.7671,
                           "source": "gps_good", "accuracy": 5.0}
    }
  ]
}
```

| キー | 内容 |
| --- | --- |
| `sensors.gravity` / `user_acceleration` | 端末座標系の重力方向と、重力を除いた加速度（G） |
| `sensors.gyro_calculated` | 姿勢角の差分から求めた角速度（rad/s）。Pythonista3 ではジャイロを直接取得できないため |
| `gps.no_signal` | GPS の更新が 5 秒以上途絶えた状態 |
| `gps.stale` | アプリ再開直後に、停止前から残っている測位であることを示す（v1.2.0 以降） |
| `resumed_after_sec` | アプリの一時停止から再開した最初のレコードでのみ、停止していた秒数（v1.2.0 以降） |
| `dead_reckoning.result` | 作動中のみ。推測位置・速度・方位（0–360°）と、最終 GPS 測位点 |
| `gps_ins_fusion.mode` | `ins`（GPS と IMU の融合）または `memory_track`（GPS 途絶直後に直前の速度・方位を保持） |

GPS の生データ（`gps.raw`）には、このほか磁気方位・真方位・各精度などの項目が含まれます。

## Features

### Dead Reckoning
GPS 信号が途絶した場合（トンネル内、屋内など）、IMU データ（加速度計・ジャイロスコープ）を積分して位置を推定します。Viewer は、DR が実際の GPS 途絶で作動したのか、アプリの一時停止からの再開で作動したのかを区別して表示します（v1.1.0 のログでは後者が記録されます）。

### Map Integration
- **記録時（iOS）**: OpenStreetMap でリアルタイム位置表示
- **再生時（Desktop）**: OpenStreetMap（既定）または国土地理院の淡色・標準・写真地図の上に、GPS 精度で色分けした航跡と、GPS 途絶時のデッドレコニング軌跡を重ねて表示

### Barometric Altimeter (未実装)
iPhoneには気圧高度計（CMAltimeter）が搭載されていますが、本アプリでは使用していません。Pythonista3環境ではCMAltimeterのコールバック処理が安定せず、アプリのクラッシュや不正確なデータ取得が発生するためです。将来的にネイティブアプリとして実装する際には対応を検討します。

## License

[MIT License](LICENSE)

## Acknowledgments

- [OpenStreetMap](https://www.openstreetmap.org/copyright) - 地図データ・タイル（© OpenStreetMap contributors、iOS / Desktop）
- [国土地理院](https://maps.gsi.go.jp/development/ichiran.html) - 地図タイル・標高タイル（Desktop）
- [Leaflet.js](https://leafletjs.com/) - 地図ライブラリ
- [PyQtGraph](https://www.pyqtgraph.org/) - グラフ描画ライブラリ
- [Pythonista 3](http://omz-software.com/pythonista/) - iOS Python IDE
