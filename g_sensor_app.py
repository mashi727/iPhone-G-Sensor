# -*- coding: utf-8 -*-
"""
Sensor Logger - Pythonista3用センサーログ記録アプリ

機能:
- モーションセンサー（加速度、ジャイロ、姿勢、磁場）
- GPS（位置、速度、精度）
- デッドレコニング（GPS途絶時の推測航法）
- 国土地理院地図（淡色）表示
- JSONログ記録

対応: iPhone 17 Pro
更新レート: 100ms (10Hz)
"""

import ui
import motion
import location
import math
import time
import json
import os
from datetime import datetime

# 共有機能
try:
    import console
    CONSOLE_AVAILABLE = True
except ImportError:
    CONSOLE_AVAILABLE = False

# スリープ防止機能・気圧計
try:
    from objc_util import ObjCClass, on_main_thread
    UIApplication = ObjCClass('UIApplication')
    UIDevice = ObjCClass('UIDevice')
    SLEEP_CONTROL_AVAILABLE = True

    # 気圧計（CMAltimeter）
    # Pythonista3では安定動作しないため無効化
    ALTIMETER_AVAILABLE = False
    CMAltimeter = None
    NSOperationQueue = None
except ImportError:
    SLEEP_CONTROL_AVAILABLE = False
    ALTIMETER_AVAILABLE = False
    UIDevice = None
    CMAltimeter = None


def set_sleep_disabled(disabled):
    """画面スリープの有効/無効を設定"""
    if SLEEP_CONTROL_AVAILABLE:
        try:
            app = UIApplication.sharedApplication()
            app.setIdleTimerDisabled_(disabled)
            return True
        except Exception as e:
            print(f'Sleep control failed: {e}')
            return False
    return False


def get_device_info():
    """デバイス情報を取得"""
    info = {
        'model': 'Unknown',
        'system_name': 'Unknown',
        'system_version': 'Unknown',
        'name': 'Unknown',
        'identifier': 'Unknown'
    }
    if UIDevice:
        try:
            device = UIDevice.currentDevice()
            info['model'] = str(device.model())
            info['system_name'] = str(device.systemName())
            info['system_version'] = str(device.systemVersion())
            info['name'] = str(device.name())
            info['identifier'] = str(device.identifierForVendor().UUIDString())
        except Exception as e:
            print(f'Device info failed: {e}')
    return info


class Barometer:
    """気圧計クラス（CMAltimeter wrapper）"""

    def __init__(self):
        self.altimeter = None
        self.current_pressure = None  # kPa
        self.relative_altitude = None  # meters
        self.is_running = False

    def start(self):
        """気圧計の更新を開始"""
        if not ALTIMETER_AVAILABLE or self.is_running:
            print('Barometer: not available or already running')
            return False

        try:
            from objc_util import ObjCInstance
            import ctypes

            self.altimeter = CMAltimeter.alloc().init()

            # 新しいオペレーションキューを作成
            queue = NSOperationQueue.alloc().init()
            queue.setName_('BarometerQueue')
            self._queue = queue  # 参照保持

            # ハンドラー内でselfを参照するためのクロージャ
            barometer_self = self

            # Pythonista3用のブロック定義
            def altitude_handler(_cmd, altitude_data, error):
                try:
                    if altitude_data:
                        data = ObjCInstance(altitude_data)
                        barometer_self.current_pressure = float(data.pressure()) * 10  # hPa
                        barometer_self.relative_altitude = float(data.relativeAltitude())
                except Exception as e:
                    print(f'Barometer handler error: {e}')

            # ブロックを作成
            from objc_util import ObjCBlock
            handler_block = ObjCBlock(
                altitude_handler,
                restype=ctypes.c_void_p,
                argtypes=[ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
            )
            self._handler_block = handler_block  # 参照保持

            self.altimeter.startRelativeAltitudeUpdatesToQueue_withHandler_(
                queue, handler_block
            )
            self.is_running = True
            print('Barometer started')
            return True
        except Exception as e:
            print(f'Barometer start failed: {e}')
            import traceback
            traceback.print_exc()
            return False

    def stop(self):
        """気圧計の更新を停止"""
        if self.altimeter and self.is_running:
            try:
                self.altimeter.stopRelativeAltitudeUpdates()
                self.is_running = False
            except Exception:
                pass

    def get_data(self):
        """現在の気圧データを取得"""
        if not self.is_running:
            return None
        return {
            'pressure_hPa': self.current_pressure,
            'relative_altitude_m': self.relative_altitude
        }


MAP_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        body { margin: 0; padding: 0; }
        #map { width: 100%; height: 100vh; }
        .leaflet-control-attribution { font-size: 8px; }
        .track-status {
            position: absolute;
            top: 10px;
            left: 50%;
            transform: translateX(-50%);
            z-index: 1000;
            background: rgba(0,0,0,0.75);
            color: #fff;
            padding: 6px 14px;
            border-radius: 16px;
            font-family: -apple-system, sans-serif;
            font-size: 12px;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .status-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            animation: pulse 1.5s infinite;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
    </style>
</head>
<body>
    <div id="map"></div>
    <div class="track-status" id="trackStatus">
        <div class="status-dot" id="statusDot"></div>
        <span id="statusText">待機中</span>
    </div>
    <script>
        // === IndexedDB タイルキャッシュ ===
        var DB_NAME = 'TileCache';
        var STORE_NAME = 'tiles';
        var db = null;

        function openDB() {
            return new Promise(function(resolve, reject) {
                if (db) { resolve(db); return; }
                var request = indexedDB.open(DB_NAME, 1);
                request.onerror = function() { reject(request.error); };
                request.onsuccess = function() { db = request.result; resolve(db); };
                request.onupgradeneeded = function(e) {
                    var database = e.target.result;
                    if (!database.objectStoreNames.contains(STORE_NAME)) {
                        database.createObjectStore(STORE_NAME);
                    }
                };
            });
        }

        function getCachedTile(key) {
            return openDB().then(function(database) {
                return new Promise(function(resolve, reject) {
                    var tx = database.transaction(STORE_NAME, 'readonly');
                    var store = tx.objectStore(STORE_NAME);
                    var request = store.get(key);
                    request.onsuccess = function() { resolve(request.result); };
                    request.onerror = function() { resolve(null); };
                });
            }).catch(function() { return null; });
        }

        function cacheTile(key, blob) {
            return openDB().then(function(database) {
                return new Promise(function(resolve) {
                    var tx = database.transaction(STORE_NAME, 'readwrite');
                    var store = tx.objectStore(STORE_NAME);
                    store.put(blob, key);
                    tx.oncomplete = function() { resolve(); };
                    tx.onerror = function() { resolve(); };
                });
            }).catch(function() {});
        }

        // キャッシュ対応タイルレイヤー
        L.TileLayer.Cached = L.TileLayer.extend({
            createTile: function(coords, done) {
                var tile = document.createElement('img');
                var url = this.getTileUrl(coords);
                var key = coords.z + '/' + coords.x + '/' + coords.y;

                tile.alt = '';
                tile.setAttribute('role', 'presentation');

                getCachedTile(key).then(function(cached) {
                    if (cached) {
                        tile.src = URL.createObjectURL(cached);
                        done(null, tile);
                    } else {
                        fetch(url).then(function(response) {
                            if (response.ok) return response.blob();
                            throw new Error('Fetch failed');
                        }).then(function(blob) {
                            cacheTile(key, blob);
                            tile.src = URL.createObjectURL(blob);
                            done(null, tile);
                        }).catch(function() {
                            tile.src = url;
                            done(null, tile);
                        });
                    }
                });
                return tile;
            }
        });

        L.tileLayer.cached = function(url, options) {
            return new L.TileLayer.Cached(url, options);
        };

        var map = L.map('map', {
            zoomControl: false
        }).setView([35.6812, 139.7671], 16);

        L.tileLayer.cached('https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png', {
            attribution: '<a href="https://maps.gsi.go.jp/development/ichiran.html">国土地理院</a>',
            maxZoom: 18
        }).addTo(map);

        // 初期位置設定（Python側から呼び出し）
        var initialPositionSet = false;
        function setInitialPosition(lat, lon) {
            if (!initialPositionSet) {
                map.setView([lat, lon], 16);
                initialPositionSet = true;
            }
        }

        // ソースタイプ別の色
        var sourceColors = {
            gps_excellent: '#06d6a0',   // 緑
            gps_good: '#118ab2',        // 青
            gps_fair: '#ffd166',        // 黄
            gps_poor: '#f77f00',        // オレンジ
            fusion: '#00CED1',          // シアン
            memory: '#FF00FF',          // マゼンタ
            ins: '#9D4EDD',             // 紫
            waiting: '#8d99ae'          // グレー
        };

        var sourceLabels = {
            gps_excellent: 'GPS 高精度',
            gps_good: 'GPS 良好',
            gps_fair: 'GPS 普通',
            gps_poor: 'GPS 低精度',
            fusion: 'GPS/INS 融合',
            memory: 'メモリートラック',
            ins: 'INS推定',
            waiting: '待機中'
        };

        // 現在位置マーカー
        var currentMarker = L.circleMarker([35.6812, 139.7671], {
            radius: 10,
            fillColor: sourceColors.waiting,
            color: '#fff',
            weight: 3,
            opacity: 0,
            fillOpacity: 0
        }).addTo(map);

        // 精度円
        var accuracyCircle = L.circle([35.6812, 139.7671], {
            radius: 50,
            fillColor: '#007AFF',
            color: '#007AFF',
            weight: 1,
            opacity: 0,
            fillOpacity: 0
        }).addTo(map);

        // 航跡セグメント（ソースタイプ別に色を変える）
        var trackSegments = [];
        var currentSegment = null;
        var currentSource = null;
        var lastPosition = null;
        var isTracking = false;

        function updateStatus(source) {
            var dot = document.getElementById('statusDot');
            var text = document.getElementById('statusText');
            dot.style.background = sourceColors[source] || sourceColors.waiting;
            text.textContent = sourceLabels[source] || source;
        }

        function updateIntegratedPosition(lat, lon, source, accuracy) {
            var latlng = L.latLng(lat, lon);

            // マーカーの色を更新
            var color = sourceColors[source] || sourceColors.waiting;
            currentMarker.setLatLng(latlng);
            currentMarker.setStyle({
                fillColor: color,
                opacity: 1,
                fillOpacity: 0.9
            });

            // 精度円（GPS系のみ表示）
            if (source.startsWith('gps') && accuracy > 0) {
                accuracyCircle.setLatLng(latlng);
                accuracyCircle.setRadius(accuracy);
                accuracyCircle.setStyle({
                    fillColor: color,
                    color: color,
                    opacity: 0.3,
                    fillOpacity: 0.1
                });
            } else {
                accuracyCircle.setStyle({opacity: 0, fillOpacity: 0});
            }

            // 航跡の追加
            if (isTracking) {
                if (currentSource !== source) {
                    // ソースが変わったら新しいセグメントを開始
                    if (lastPosition) {
                        // 前のセグメントに現在位置を追加（つなぐため）
                        if (currentSegment) {
                            currentSegment.addLatLng(latlng);
                        }
                    }
                    // 新しいセグメントを開始
                    currentSegment = L.polyline([latlng], {
                        color: color,
                        weight: 4,
                        opacity: 0.9,
                        lineCap: 'round',
                        lineJoin: 'round'
                    }).addTo(map);
                    trackSegments.push(currentSegment);
                    currentSource = source;
                } else {
                    // 同じソースなら現在のセグメントに追加
                    if (currentSegment) {
                        currentSegment.addLatLng(latlng);
                    }
                }
            }

            lastPosition = latlng;
            map.setView(latlng);
            updateStatus(source);
        }

        function startTracking() {
            isTracking = true;
            currentSource = null;
            currentSegment = null;
            updateStatus('waiting');
        }

        function stopTracking() {
            isTracking = false;
        }

        function resetTrack() {
            // 全セグメントを削除
            trackSegments.forEach(function(seg) {
                map.removeLayer(seg);
            });
            trackSegments = [];
            currentSegment = null;
            currentSource = null;
            lastPosition = null;
            isTracking = false;

            // マーカーを非表示
            currentMarker.setStyle({opacity: 0, fillOpacity: 0});
            accuracyCircle.setStyle({opacity: 0, fillOpacity: 0});
            updateStatus('waiting');
        }
    </script>
</body>
</html>
'''


class DataLogger:
    """センサーデータのログ記録クラス"""

    def __init__(self):
        self.session_start = datetime.now()
        self.records = []
        device_info = get_device_info()
        self.metadata = {
            'session_start': self.session_start.isoformat(),
            'session_start_unix': time.time(),
            'device': device_info.get('model', 'iPhone'),
            'device_info': device_info,
            'app_version': '1.1.0',
            'update_interval_ms': 100,
            'sensors_available': {
                'altimeter': ALTIMETER_AVAILABLE,
                'sleep_control': SLEEP_CONTROL_AVAILABLE,
                'direct_gyro': False  # Pythonista3では直接取得不可
            }
        }
        self.last_saved_path = None

    def add_record(self, data):
        """レコードを追加"""
        record = {
            'timestamp': time.time(),
            'datetime': datetime.now().isoformat(),
            'sequence': len(self.records),
            **data
        }
        self.records.append(record)

    def _get_log_data(self):
        """保存用データを取得"""
        return {
            'metadata': self.metadata,
            'record_count': len(self.records),
            'records': self.records
        }

    def _get_filename(self):
        """ファイル名を生成"""
        return f"sensor_log_{self.session_start.strftime('%Y%m%d_%H%M%S')}.json"

    def save(self, directory=None):
        """ローカルに保存"""
        if directory is None:
            directory = os.path.expanduser('~/Documents')

        log_dir = os.path.join(directory, 'sensor_logs')
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)

        filename = self._get_filename()
        filepath = os.path.join(log_dir, filename)

        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(self._get_log_data(), f, ensure_ascii=False, separators=(',', ':'))

        self.last_saved_path = filepath
        return filepath

    def share(self):
        """共有シートを開く（Dropbox等に送信可能）"""
        if not self.last_saved_path:
            return False

        if CONSOLE_AVAILABLE:
            try:
                console.open_in(self.last_saved_path)
                return True
            except Exception as e:
                print(f'Share failed: {e}')
                return False
        return False

    def get_record_count(self):
        """記録数を取得"""
        return len(self.records)


class GPSINSFusion:
    """GPS/INS融合による位置推定クラス（簡易Kalmanフィルタ + メモリートラック）"""

    EARTH_RADIUS = 6378137.0

    # メモリートラック設定
    ACCURACY_THRESHOLD_GOOD = 15.0   # これ以下なら速度を記憶
    ACCURACY_THRESHOLD_DEGRADE = 30.0  # これ以上でメモリートラック発動
    MEMORY_VELOCITY_DECAY = 0.98     # メモリー速度の減衰率（per update）
    MEMORY_MAX_DURATION = 60.0       # メモリートラック最大持続時間（秒）

    def __init__(self):
        self.reset()

    def reset(self):
        """状態をリセット"""
        self.current_lat = None
        self.current_lon = None
        self.velocity_north = 0.0
        self.velocity_east = 0.0
        self.position_uncertainty = 10.0
        self.velocity_uncertainty = 1.0
        self.track = []  # [(lat, lon), ...]
        self.is_initialized = False
        self.last_yaw = None
        self.current_heading = 0.0

        # メモリートラック用
        self.memory_velocity_north = 0.0
        self.memory_velocity_east = 0.0
        self.memory_heading = 0.0
        self.memory_speed = 0.0
        self.is_memory_mode = False
        self.memory_mode_start_time = None
        self.last_good_gps_time = None

    def initialize(self, lat, lon):
        """初期位置を設定"""
        self.current_lat = lat
        self.current_lon = lon
        self.track = [(lat, lon)]
        self.is_initialized = True

    def update_gps(self, lat, lon, speed, course, accuracy):
        """GPS観測で状態を更新（測定更新）"""
        if not self.is_initialized:
            self.initialize(lat, lon)
            return

        current_time = time.time()

        # GPS精度が良好な場合：速度をメモリに記憶
        if accuracy >= 0 and accuracy < self.ACCURACY_THRESHOLD_GOOD:
            if course >= 0 and speed > 0.3:
                course_rad = math.radians(course)
                self.memory_velocity_north = speed * math.cos(course_rad)
                self.memory_velocity_east = speed * math.sin(course_rad)
                self.memory_heading = course_rad
                self.memory_speed = speed
            self.last_good_gps_time = current_time

            # メモリーモード解除
            if self.is_memory_mode:
                self.is_memory_mode = False
                self.memory_mode_start_time = None

        # GPS精度が悪化した場合：メモリートラックモードへ
        if accuracy < 0 or accuracy >= self.ACCURACY_THRESHOLD_DEGRADE:
            if not self.is_memory_mode and self.memory_speed > 0.3:
                self.is_memory_mode = True
                self.memory_mode_start_time = current_time
            return  # GPS更新をスキップ

        # 通常のGPS更新処理
        # GPS精度に基づく重み（精度が高いほど重みが大きい）
        gps_weight = 1.0 / (1.0 + accuracy / 10.0)

        # 位置の補正
        self.current_lat = (1 - gps_weight) * self.current_lat + gps_weight * lat
        self.current_lon = (1 - gps_weight) * self.current_lon + gps_weight * lon

        # 速度の補正（GPSのcourseが有効な場合）
        if course >= 0 and speed > 0.5:
            course_rad = math.radians(course)
            gps_vel_north = speed * math.cos(course_rad)
            gps_vel_east = speed * math.sin(course_rad)

            vel_weight = gps_weight * 0.5
            self.velocity_north = (1 - vel_weight) * self.velocity_north + vel_weight * gps_vel_north
            self.velocity_east = (1 - vel_weight) * self.velocity_east + vel_weight * gps_vel_east
            self.current_heading = course_rad

        # 不確実性の更新
        self.position_uncertainty = accuracy * 0.5 + self.position_uncertainty * 0.5
        self.velocity_uncertainty *= 0.9

        # 軌跡に追加
        self.track.append((self.current_lat, self.current_lon))

    def update_ins(self, user_accel, attitude, dt):
        """INS（センサー）データで状態を予測更新"""
        if not self.is_initialized or self.current_lat is None:
            return None

        current_time = time.time()
        use_memory_track = False
        memory_elapsed = 0.0

        # メモリートラックモードの判定
        if self.is_memory_mode and self.memory_mode_start_time:
            memory_elapsed = current_time - self.memory_mode_start_time
            if memory_elapsed < self.MEMORY_MAX_DURATION and self.memory_speed > 0.3:
                use_memory_track = True

        if use_memory_track:
            # === メモリートラックモード ===
            # 記憶した速度で等速直線運動を仮定

            # ジャイロで方位変化のみ検出（旋回対応）
            if attitude:
                roll, pitch, yaw = attitude
                if self.last_yaw is not None:
                    delta_yaw = yaw - self.last_yaw
                    if delta_yaw > math.pi:
                        delta_yaw -= 2 * math.pi
                    elif delta_yaw < -math.pi:
                        delta_yaw += 2 * math.pi
                    # 方位変化をメモリ速度に適用
                    self.memory_heading += delta_yaw
                self.last_yaw = yaw

            # メモリ速度を減衰（時間経過で信頼度低下）
            self.memory_velocity_north *= self.MEMORY_VELOCITY_DECAY
            self.memory_velocity_east *= self.MEMORY_VELOCITY_DECAY
            self.memory_speed *= self.MEMORY_VELOCITY_DECAY

            # 方位変化を反映した速度ベクトル
            vel_north = self.memory_speed * math.cos(self.memory_heading)
            vel_east = self.memory_speed * math.sin(self.memory_heading)

            # 位置の更新（メモリ速度使用）
            delta_lat = (vel_north * dt) / self.EARTH_RADIUS
            delta_lon = (vel_east * dt) / (
                self.EARTH_RADIUS * math.cos(math.radians(self.current_lat))
            )

            self.current_lat += math.degrees(delta_lat)
            self.current_lon += math.degrees(delta_lon)

            speed = self.memory_speed

        else:
            # === 通常INSモード ===
            # ヨー角の変化から方位を更新
            if attitude:
                roll, pitch, yaw = attitude

                if self.last_yaw is not None:
                    delta_yaw = yaw - self.last_yaw
                    if delta_yaw > math.pi:
                        delta_yaw -= 2 * math.pi
                    elif delta_yaw < -math.pi:
                        delta_yaw += 2 * math.pi
                    self.current_heading += delta_yaw

                self.last_yaw = yaw

                # ピッチ補正
                cos_pitch = math.cos(pitch)
                sin_pitch = math.sin(pitch)
                cos_roll = math.cos(roll)

            # 加速度から世界座標系への変換
            if user_accel and attitude:
                ax, ay, az = user_accel

                # ピッチ・ロール補正
                ay_corrected = ay * cos_pitch - az * sin_pitch
                ax_corrected = ax * cos_roll

                # デバイス座標から世界座標へ
                heading = 3 * math.pi / 2 - yaw

                accel_forward = ay_corrected
                accel_right = ax_corrected

                accel_north = (accel_forward * math.cos(heading) -
                              accel_right * math.sin(heading))
                accel_east = (accel_forward * math.sin(heading) +
                             accel_right * math.cos(heading))

                # 静止検出（ZUPT）
                accel_mag = math.sqrt(ax**2 + ay**2 + az**2)
                is_stationary = accel_mag < 0.08

                if is_stationary:
                    self.velocity_north *= 0.8
                    self.velocity_east *= 0.8
                else:
                    threshold = 0.05
                    if abs(ax) > threshold or abs(ay) > threshold:
                        self.velocity_north += accel_north * 9.81 * dt
                        self.velocity_east += accel_east * 9.81 * dt

            # 速度の減衰（ドリフト抑制）
            self.velocity_north *= 0.99
            self.velocity_east *= 0.99

            # 最大速度制限
            max_speed = 10.0
            speed = math.sqrt(self.velocity_north**2 + self.velocity_east**2)
            if speed > max_speed:
                scale = max_speed / speed
                self.velocity_north *= scale
                self.velocity_east *= scale

            # 位置の更新
            delta_lat = (self.velocity_north * dt) / self.EARTH_RADIUS
            delta_lon = (self.velocity_east * dt) / (
                self.EARTH_RADIUS * math.cos(math.radians(self.current_lat))
            )

            self.current_lat += math.degrees(delta_lat)
            self.current_lon += math.degrees(delta_lon)

        # 不確実性の増加（予測ステップでは増加）
        self.position_uncertainty += 0.1 * dt
        self.velocity_uncertainty += 0.05 * dt

        # 軌跡に追加
        self.track.append((self.current_lat, self.current_lon))

        return {
            'lat': self.current_lat,
            'lon': self.current_lon,
            'speed': speed,
            'heading': math.degrees(self.current_heading if not use_memory_track else self.memory_heading),
            'mode': 'memory_track' if use_memory_track else 'ins',
            'memory_elapsed': memory_elapsed if use_memory_track else 0.0
        }

    def get_track(self):
        """軌跡を取得"""
        return self.track


class DeadReckoning:
    """デッドレコニング（推測航法）クラス"""

    EARTH_RADIUS = 6378137.0

    def __init__(self):
        self.reset()

    def reset(self):
        """状態をリセット"""
        self.last_gps_lat = None
        self.last_gps_lon = None
        self.last_gps_speed = 0.0
        self.last_gps_course = 0.0
        self.last_gps_time = None

        self.current_lat = None
        self.current_lon = None
        self.velocity_north = 0.0
        self.velocity_east = 0.0

        self.last_yaw = None
        self.current_heading = 0.0

        self.is_active = False
        self.dr_start_time = None

    def update_gps(self, lat, lon, speed, course, timestamp):
        """GPS位置を更新"""
        self.last_gps_lat = lat
        self.last_gps_lon = lon
        self.last_gps_speed = speed
        self.last_gps_course = course
        self.last_gps_time = timestamp

        self.current_lat = lat
        self.current_lon = lon

        course_rad = math.radians(course)
        self.velocity_north = speed * math.cos(course_rad)
        self.velocity_east = speed * math.sin(course_rad)
        self.current_heading = course_rad

        self.is_active = False
        self.dr_start_time = None

    def start_dead_reckoning(self):
        """デッドレコニング開始"""
        if self.last_gps_lat is None:
            return False

        self.is_active = True
        self.dr_start_time = time.time()
        self.last_update_time = time.time()
        return True

    def update_with_sensors(self, user_accel, attitude, dt):
        """センサーデータで位置を更新"""
        if not self.is_active or self.current_lat is None:
            return None

        delta_yaw = 0.0

        if attitude:
            roll, pitch, yaw = attitude

            if self.last_yaw is not None:
                delta_yaw = yaw - self.last_yaw

                if delta_yaw > math.pi:
                    delta_yaw -= 2 * math.pi
                elif delta_yaw < -math.pi:
                    delta_yaw += 2 * math.pi

                self.current_heading += delta_yaw

            self.last_yaw = yaw

        accel_north = 0.0
        accel_east = 0.0

        if user_accel:
            ax, ay, az = user_accel

            accel_forward = ay
            accel_right = ax

            accel_north = (accel_forward * math.cos(self.current_heading)
                          - accel_right * math.sin(self.current_heading))
            accel_east = (accel_forward * math.sin(self.current_heading)
                         + accel_right * math.cos(self.current_heading))

            threshold = 0.05
            if abs(ax) > threshold or abs(ay) > threshold:
                self.velocity_north += accel_north * 9.81 * dt
                self.velocity_east += accel_east * 9.81 * dt

        decay = 0.995
        self.velocity_north *= decay
        self.velocity_east *= decay

        speed = math.sqrt(self.velocity_north**2 + self.velocity_east**2)

        delta_lat = (self.velocity_north * dt) / self.EARTH_RADIUS
        delta_lon = (self.velocity_east * dt) / (
            self.EARTH_RADIUS * math.cos(math.radians(self.current_lat))
        )

        self.current_lat += math.degrees(delta_lat)
        self.current_lon += math.degrees(delta_lon)

        return {
            'lat': self.current_lat,
            'lon': self.current_lon,
            'speed': speed,
            'heading': math.degrees(self.current_heading),
            'elapsed': time.time() - self.dr_start_time,
            # デバッグ用の計算値
            'debug': {
                'velocity_north': self.velocity_north,
                'velocity_east': self.velocity_east,
                'delta_yaw': delta_yaw,
                'accel_north': accel_north,
                'accel_east': accel_east,
                'delta_lat': math.degrees(delta_lat),
                'delta_lon': math.degrees(delta_lon)
            }
        }


class SensorView(ui.View):
    """センサー値を表示するメインビュー"""

    def __init__(self):
        super().__init__()
        self.name = 'Sensor Logger'
        self.background_color = '#1a1a2e'
        self.update_interval = 0.1
        self.sensor_labels = {}
        self._prev_attitude = None
        self._map_initialized = False
        self._map_ready = False
        self._start_time = time.time()
        self._last_gps_timestamp = None
        self._gps_timeout = 5.0

        self.dead_reckoning = DeadReckoning()
        self._dr_mode = False

        # GPS/INS融合
        self.gps_ins_fusion = GPSINSFusion()

        # 気圧計
        self.barometer = Barometer()

        # ログ記録（初期状態: 停止）
        self.logger = DataLogger()
        self._logging_enabled = False

        self._setup_ui()
        self._start_updates()

    def _create_section(self, title, items, x_pos, y_pos, color, compact=False):
        """セクションの作成"""
        w = 195
        padding = 8

        header = ui.Label()
        header.text = title
        header.font = ('Menlo-Bold', 12)
        header.text_color = color
        header.frame = (x_pos + padding, y_pos, w - padding, 16)
        self.add_subview(header)

        y = y_pos + 17
        row_height = 18

        labels = {}
        for key, label_text, unit, fmt in items:
            lbl = ui.Label()
            lbl.text = f'{label_text}:'
            lbl.font = ('Menlo', 12)
            lbl.text_color = '#8d99ae'
            lbl.frame = (x_pos + padding, y, 50, row_height)
            self.add_subview(lbl)

            val = ui.Label()
            val.text = f'--- {unit}'
            val.font = ('Menlo', 12)
            val.text_color = '#edf2f4'
            val.alignment = ui.ALIGN_RIGHT
            val.frame = (x_pos + 55, y, w - 60, row_height)
            self.add_subview(val)

            labels[key] = {'value': val, 'unit': unit, 'fmt': fmt}
            y += row_height

        return labels, y + 3

    def _setup_ui(self):
        """UIコンポーネントの初期化"""

        # コントロールパネル（ボタン類のコンテナ）
        self.control_panel = ui.View()
        self.control_panel.background_color = '#2d2d44'
        self.add_subview(self.control_panel)

        # 記録ボタン（初期状態: 停止）
        self.rec_button = ui.Button()
        self.rec_button.title = '● START'
        self.rec_button.font = ('Menlo-Bold', 16)
        self.rec_button.background_color = '#06d6a0'
        self.rec_button.tint_color = '#ffffff'
        self.rec_button.corner_radius = 8
        self.rec_button.action = self._toggle_recording
        self.control_panel.add_subview(self.rec_button)

        # 共有ボタン
        self.share_button = ui.Button()
        self.share_button.title = 'SHARE'
        self.share_button.font = ('Menlo-Bold', 16)
        self.share_button.background_color = '#8d99ae'
        self.share_button.tint_color = '#ffffff'
        self.share_button.corner_radius = 8
        self.share_button.action = self._share_log
        self.share_button.enabled = False
        self.control_panel.add_subview(self.share_button)

        # ログ記録インジケータ
        self.log_label = ui.Label()
        self.log_label.text = '0 rec'
        self.log_label.font = ('Menlo-Bold', 14)
        self.log_label.text_color = '#8d99ae'
        self.log_label.alignment = ui.ALIGN_CENTER
        self.control_panel.add_subview(self.log_label)

        self.map_view = ui.WebView()
        self.map_view.scales_page_to_fit = False
        self.map_view.load_html(MAP_HTML)
        self.add_subview(self.map_view)

    def _build_sections(self):
        """セクションの構築"""
        left_x = 0
        right_x = 200
        # 画面上部から開始
        y_left = 12
        y_right = 12

        items = [
            ('X', 'X', 'G', '+.3f'), ('Y', 'Y', 'G', '+.3f'),
            ('Z', 'Z', 'G', '+.3f'), ('mag', '|G|', 'G', '.3f')
        ]
        self.sensor_labels['gravity'], y_left = self._create_section(
            'GRAVITY', items, left_x, y_left, '#ef476f'
        )

        items = [
            ('X', 'X', 'G', '+.3f'), ('Y', 'Y', 'G', '+.3f'),
            ('Z', 'Z', 'G', '+.3f'), ('mag', '|A|', 'G', '.3f')
        ]
        self.sensor_labels['user_accel'], y_left = self._create_section(
            'USER ACCEL', items, left_x, y_left, '#06d6a0'
        )

        items = [
            ('X', 'X', 'r/s', '+.2f'), ('Y', 'Y', 'r/s', '+.2f'),
            ('Z', 'Z', 'r/s', '+.2f')
        ]
        self.sensor_labels['gyro'], y_left = self._create_section(
            'GYRO', items, left_x, y_left, '#118ab2'
        )

        items = [
            ('roll', 'Roll', '°', '+.1f'), ('pitch', 'Ptch', '°', '+.1f'),
            ('yaw', 'Yaw', '°', '+.1f')
        ]
        self.sensor_labels['attitude'], y_right = self._create_section(
            'ATTITUDE', items, right_x, y_right, '#ffd166'
        )

        items = [
            ('X', 'X', 'μT', '+.1f'), ('Y', 'Y', 'μT', '+.1f'),
            ('Z', 'Z', 'μT', '+.1f'), ('mag', '|B|', 'μT', '.1f')
        ]
        self.sensor_labels['magnetic'], y_right = self._create_section(
            'MAGNETIC', items, right_x, y_right, '#9d4edd'
        )

        items = [
            ('lat', 'Lat', '°', '.5f'), ('lon', 'Lon', '°', '.5f'),
            ('alt', 'Alt', 'm', '.1f'), ('speed', 'Spd', 'm/s', '.1f')
        ]
        self.sensor_labels['gps'], y_right = self._create_section(
            'GPS', items, right_x, y_right, '#00b4d8'
        )

        self.gps_status_label = ui.Label()
        self.gps_status_label.text = '--- (--m)'
        self.gps_status_label.font = ('Menlo-Bold', 13)
        self.gps_status_label.text_color = '#8d99ae'
        self.gps_status_label.alignment = ui.ALIGN_CENTER
        self.gps_status_label.frame = (right_x + 8, y_right, 190, 18)
        self.add_subview(self.gps_status_label)
        y_right += 20

        return max(y_left, y_right)

    def layout(self):
        """レイアウト設定"""
        w, h = self.width, self.height

        if not self.sensor_labels:
            sensor_bottom = self._build_sections()
        else:
            sensor_bottom = 291

        # コントロールパネル（センサー表示と地図の間）
        panel_height = 50
        panel_top = sensor_bottom + 5
        self.control_panel.frame = (0, panel_top, w, panel_height)

        # ボタンレイアウト
        btn_height = 38
        btn_y = (panel_height - btn_height) / 2
        btn_padding = 10

        self.rec_button.frame = (btn_padding, btn_y, 140, btn_height)
        self.share_button.frame = (160, btn_y, 100, btn_height)
        self.log_label.frame = (270, btn_y, w - 280, btn_height)

        # 地図エリア
        map_top = panel_top + panel_height
        self.map_view.frame = (0, map_top, w, h - map_top)

    def _toggle_recording(self, sender):
        """記録の開始/停止を切り替え"""
        if self._logging_enabled:
            # 記録停止
            self._logging_enabled = False
            self.rec_button.title = '● START'
            self.rec_button.background_color = '#06d6a0'

            # 航跡記録停止
            self._stop_map_tracking()

            # 気圧計停止
            self.barometer.stop()

            # スリープ防止解除
            set_sleep_disabled(False)
            print('Sleep timer enabled')

            # ログ保存
            if self.logger.get_record_count() > 0:
                filepath = self.logger.save()
                print(f'Log saved: {filepath}')
                # 保存完了表示
                self.log_label.text = 'Saved!'
                self.log_label.text_color = '#06d6a0'
                # SHAREボタン有効化
                self.share_button.enabled = True
                self.share_button.background_color = '#118ab2'
        else:
            # 記録開始（新しいロガーを作成）
            self.logger = DataLogger()
            self._logging_enabled = True
            self.rec_button.title = '■ STOP'
            self.rec_button.background_color = '#ef476f'
            self.log_label.text = '0 rec'
            self.log_label.text_color = '#ef476f'
            # SHAREボタン無効化
            self.share_button.enabled = False
            self.share_button.background_color = '#8d99ae'

            # GPS/INS Fusionをリセット
            self.gps_ins_fusion.reset()
            # 航跡をリセットして記録開始
            self._reset_map_track()
            self._start_map_tracking()

            # 気圧計開始
            if self.barometer.start():
                print('Barometer started')

            # スリープ防止ON
            set_sleep_disabled(True)
            print('Sleep timer disabled (screen will stay on)')

    def _share_log(self, sender):
        """ログファイルを共有"""
        if self.logger.share():
            print('Share dialog opened')
        else:
            print('Share failed')

    def _start_updates(self):
        """センサーの更新開始"""
        motion.start_updates()
        location.start_updates()
        self._last_update_time = time.time()
        self._schedule_update()

    def _schedule_update(self):
        """定期更新のスケジューリング"""
        ui.delay(self._update_display, self.update_interval)

    def _update_value(self, group, key, value):
        """センサー値の表示更新"""
        if group not in self.sensor_labels:
            return
        if key not in self.sensor_labels[group]:
            return
        item = self.sensor_labels[group][key]
        unit = item['unit']
        fmt = item['fmt']
        item['value'].text = f'{value:{fmt}} {unit}'

    def _update_map_integrated(self, lat, lon, source, accuracy=0):
        """地図上の統合航跡位置を更新"""
        js = f'updateIntegratedPosition({lat}, {lon}, "{source}", {accuracy});'
        self.map_view.evaluate_javascript(js)

    def _start_map_tracking(self):
        """地図の航跡記録を開始"""
        js = 'startTracking();'
        self.map_view.evaluate_javascript(js)

    def _stop_map_tracking(self):
        """地図の航跡記録を停止"""
        js = 'stopTracking();'
        self.map_view.evaluate_javascript(js)

    def _reset_map_track(self):
        """地図の航跡をリセット"""
        js = 'resetTrack();'
        self.map_view.evaluate_javascript(js)

    def _update_gps_status(self, h_acc, timestamp=None, is_dr=False, dr_elapsed=0):
        """GPS受信状況を更新"""
        now = time.time()

        if is_dr:
            status = f'DR {dr_elapsed:.0f}s'
            color = '#FF9500'
            self.gps_status_label.text = status
            self.gps_status_label.text_color = color
            return

        if timestamp is not None:
            if self._last_gps_timestamp != timestamp:
                self._last_gps_timestamp = timestamp
                self._last_gps_update_time = now

        if not hasattr(self, '_last_gps_update_time'):
            self._last_gps_update_time = now

        time_since_update = now - self._last_gps_update_time

        if timestamp is None or time_since_update > self._gps_timeout:
            status = 'No Signal'
            color = '#ef476f'
            self.gps_status_label.text = status
            self.gps_status_label.text_color = color
            return True

        if h_acc < 0:
            status = 'Invalid'
            color = '#ef476f'
        elif h_acc < 5:
            status = 'Excellent'
            color = '#06d6a0'
        elif h_acc < 15:
            status = 'Good'
            color = '#ffd166'
        elif h_acc < 30:
            status = 'Fair'
            color = '#f77f00'
        elif h_acc < 100:
            status = 'Poor'
            color = '#ef476f'
        else:
            status = 'Very Poor'
            color = '#ef476f'

        self.gps_status_label.text = f'{status} ({h_acc:.0f}m)'
        self.gps_status_label.text_color = color
        return False

    def _update_display(self):
        """表示の更新"""
        if not self.on_screen:
            return

        now = time.time()
        dt = now - self._last_update_time
        self._last_update_time = now

        # センサーデータ取得
        gravity = motion.get_gravity()
        user_accel = motion.get_user_acceleration()
        attitude = motion.get_attitude()
        magnetic = motion.get_magnetic_field()
        loc = location.get_location()

        # 気圧計データ取得
        barometer_data = self.barometer.get_data()

        # ログ用データ構造（拡張版）
        log_record = {
            'dt': dt,
            'sensors': {
                'gravity': None,
                'user_acceleration': None,
                'raw_acceleration': None,  # 重力含む生加速度
                'attitude': None,
                'magnetic_field': None,
                'gyro_calculated': None,  # 姿勢差分から計算した角速度
                'barometer': barometer_data  # 気圧・相対高度
            },
            'gps': {
                'raw': None,
                'status': None,
                'no_signal': False
            },
            'dead_reckoning': {
                'active': False,
                'result': None
            }
        }

        # 重力加速度
        if gravity:
            x, y, z = gravity
            self._update_value('gravity', 'X', x)
            self._update_value('gravity', 'Y', y)
            self._update_value('gravity', 'Z', z)
            mag = math.sqrt(x**2 + y**2 + z**2)
            self._update_value('gravity', 'mag', mag)

            log_record['sensors']['gravity'] = {
                'x': x, 'y': y, 'z': z, 'magnitude': mag
            }

        # ユーザー加速度
        if user_accel:
            x, y, z = user_accel
            self._update_value('user_accel', 'X', x)
            self._update_value('user_accel', 'Y', y)
            self._update_value('user_accel', 'Z', z)
            mag = math.sqrt(x**2 + y**2 + z**2)
            self._update_value('user_accel', 'mag', mag)

            log_record['sensors']['user_acceleration'] = {
                'x': x, 'y': y, 'z': z, 'magnitude': mag
            }

        # 生加速度（重力 + ユーザー加速度）- センサーフュージョン前処理用
        if gravity and user_accel:
            raw_x = gravity[0] + user_accel[0]
            raw_y = gravity[1] + user_accel[1]
            raw_z = gravity[2] + user_accel[2]
            raw_mag = math.sqrt(raw_x**2 + raw_y**2 + raw_z**2)
            log_record['sensors']['raw_acceleration'] = {
                'x': raw_x, 'y': raw_y, 'z': raw_z, 'magnitude': raw_mag
            }

        # 姿勢（オイラー角）
        gyro_data = None
        if attitude:
            roll, pitch, yaw = attitude
            self._update_value('attitude', 'roll', math.degrees(roll))
            self._update_value('attitude', 'pitch', math.degrees(pitch))
            self._update_value('attitude', 'yaw', math.degrees(yaw))

            log_record['sensors']['attitude'] = {
                'roll_rad': roll, 'pitch_rad': pitch, 'yaw_rad': yaw,
                'roll_deg': math.degrees(roll),
                'pitch_deg': math.degrees(pitch),
                'yaw_deg': math.degrees(yaw)
            }

            # 角速度の近似計算
            if self._prev_attitude:
                gyro_x = (roll - self._prev_attitude[0]) / dt
                gyro_y = (pitch - self._prev_attitude[1]) / dt
                gyro_z = (yaw - self._prev_attitude[2]) / dt
                self._update_value('gyro', 'X', gyro_x)
                self._update_value('gyro', 'Y', gyro_y)
                self._update_value('gyro', 'Z', gyro_z)

                gyro_data = {'x': gyro_x, 'y': gyro_y, 'z': gyro_z}
                log_record['sensors']['gyro_calculated'] = gyro_data

            self._prev_attitude = attitude

        # 磁場
        if magnetic:
            x, y, z = magnetic[:3]
            accuracy = magnetic[3] if len(magnetic) > 3 else -1
            self._update_value('magnetic', 'X', x)
            self._update_value('magnetic', 'Y', y)
            self._update_value('magnetic', 'Z', z)
            mag = math.sqrt(x**2 + y**2 + z**2)
            self._update_value('magnetic', 'mag', mag)

            log_record['sensors']['magnetic_field'] = {
                'x': x, 'y': y, 'z': z,
                'magnitude': mag, 'accuracy': accuracy
            }

        # GPS
        no_signal = False
        gps_status = None

        if loc:
            lat = loc.get('latitude', 0)
            lon = loc.get('longitude', 0)
            alt = loc.get('altitude', 0)
            speed = max(0, loc.get('speed', 0))
            course = loc.get('course', 0)
            h_acc = loc.get('horizontal_accuracy', 50)
            v_acc = loc.get('vertical_accuracy', -1)
            timestamp = loc.get('timestamp', None)

            # 磁気方位・真方位を取得（利用可能な場合）
            magnetic_heading = loc.get('magnetic_heading', -1)
            true_heading = loc.get('true_heading', -1)
            heading_accuracy = loc.get('heading_accuracy', -1)

            # 追加のGPS/位置情報（利用可能な場合）
            floor = loc.get('floor', None)  # 屋内階数
            speed_accuracy = loc.get('speed_accuracy', -1)  # 速度精度
            course_accuracy = loc.get('course_accuracy', -1)  # 進行方向精度

            log_record['gps']['raw'] = {
                'latitude': lat,
                'longitude': lon,
                'altitude': alt,
                'speed': loc.get('speed', 0),
                'speed_clamped': speed,
                'course': course,
                'horizontal_accuracy': h_acc,
                'vertical_accuracy': v_acc,
                'timestamp': timestamp,
                'magnetic_heading': magnetic_heading,
                'true_heading': true_heading,
                'heading_accuracy': heading_accuracy,
                'floor': floor,
                'speed_accuracy': speed_accuracy,
                'course_accuracy': course_accuracy
            }

            # デバッグ用: 生のlocationデータ全体を保存
            log_record['gps']['raw_location_dict'] = dict(loc)

            no_signal = self._update_gps_status(h_acc, timestamp)

            if h_acc < 5:
                gps_status = 'excellent'
            elif h_acc < 15:
                gps_status = 'good'
            elif h_acc < 30:
                gps_status = 'fair'
            elif h_acc < 100:
                gps_status = 'poor'
            else:
                gps_status = 'very_poor'

            log_record['gps']['status'] = gps_status
            log_record['gps']['no_signal'] = no_signal

            if not no_signal:
                self._dr_mode = False
                self.dead_reckoning.update_gps(lat, lon, speed, course, timestamp)

                # 初回GPS取得時に地図を現在位置に移動
                # WebViewのロード完了を待つ（起動から2秒後）
                elapsed = time.time() - self._start_time
                if not self._map_initialized and elapsed > 2.0:
                    js = f'setInitialPosition({lat}, {lon});'
                    self.map_view.evaluate_javascript(js)
                    self._map_initialized = True

                # GPS/INS Fusion: GPS更新（ログ記録中のみ）
                if self._logging_enabled:
                    self.gps_ins_fusion.update_gps(lat, lon, speed, course, h_acc)

                self._update_value('gps', 'lat', lat)
                self._update_value('gps', 'lon', lon)
                self._update_value('gps', 'alt', alt)
                self._update_value('gps', 'speed', speed)

                self.sensor_labels['gps']['lat']['value'].text_color = '#edf2f4'
                self.sensor_labels['gps']['lon']['value'].text_color = '#edf2f4'
                self.sensor_labels['gps']['speed']['value'].text_color = '#edf2f4'
        else:
            no_signal = True
            self._update_gps_status(-1, None)
            log_record['gps']['no_signal'] = True
            log_record['gps']['status'] = 'no_signal'

        # デッドレコニングモード（ログ用に継続）
        if no_signal and self.dead_reckoning.last_gps_lat is not None:
            if not self._dr_mode:
                self._dr_mode = True
                self.dead_reckoning.start_dead_reckoning()

            dr_result = self.dead_reckoning.update_with_sensors(
                user_accel, attitude, dt
            )

            if dr_result:
                log_record['dead_reckoning']['active'] = True
                log_record['dead_reckoning']['result'] = {
                    'latitude': dr_result['lat'],
                    'longitude': dr_result['lon'],
                    'speed': dr_result['speed'],
                    'heading_deg': dr_result['heading'],
                    'elapsed_sec': dr_result['elapsed'],
                    'last_gps_lat': self.dead_reckoning.last_gps_lat,
                    'last_gps_lon': self.dead_reckoning.last_gps_lon,
                    'debug': dr_result.get('debug', {})
                }

        # GPS/INS Fusion: INS更新（ログ記録中のみ）
        fusion_result = None
        if self._logging_enabled and self.gps_ins_fusion.is_initialized:
            fusion_result = self.gps_ins_fusion.update_ins(user_accel, attitude, dt)
            if fusion_result:
                # ログにFusion結果を追加
                log_record['gps_ins_fusion'] = {
                    'latitude': fusion_result['lat'],
                    'longitude': fusion_result['lon'],
                    'speed': fusion_result['speed'],
                    'heading': fusion_result['heading'],
                    'mode': fusion_result.get('mode', 'ins'),
                    'memory_elapsed': fusion_result.get('memory_elapsed', 0.0)
                }

        # === 統合航跡の表示（ログ記録中のみ） ===
        # 常にFusion位置を使用（連続性確保）、色はGPS精度/モードで決定
        if self._logging_enabled:
            display_lat, display_lon, track_source, display_acc = None, None, None, 0

            if fusion_result:
                # 常にFusion位置を使用
                display_lat = fusion_result['lat']
                display_lon = fusion_result['lon']
                fusion_mode = fusion_result.get('mode', 'ins')

                # 色の決定: GPS精度が良ければGPS色、そうでなければFusion/Memory/INS色
                if not no_signal and loc:
                    display_acc = h_acc
                    if h_acc < 5:
                        track_source = 'gps_excellent'
                    elif h_acc < 15:
                        track_source = 'gps_good'
                    elif h_acc < 30:
                        track_source = 'gps_fair'
                    else:
                        # GPS精度が悪い場合はFusionモードで色分け
                        track_source = 'memory' if fusion_mode == 'memory_track' else 'fusion'
                else:
                    # GPS無効時はFusionモードで色分け
                    track_source = 'memory' if fusion_mode == 'memory_track' else 'ins'

            elif not no_signal and loc:
                # Fusionがない場合はGPSを使用（後方互換性）
                display_lat, display_lon = lat, lon
                display_acc = h_acc
                if h_acc < 5:
                    track_source = 'gps_excellent'
                elif h_acc < 15:
                    track_source = 'gps_good'
                elif h_acc < 30:
                    track_source = 'gps_fair'
                else:
                    track_source = 'gps_poor'

            # 地図を更新
            if display_lat is not None and display_lon is not None:
                self._update_map_integrated(display_lat, display_lon, track_source, display_acc)

                # ログに統合航跡情報を追加
                log_record['integrated_track'] = {
                    'latitude': display_lat,
                    'longitude': display_lon,
                    'source': track_source,
                    'accuracy': display_acc
                }

        # ログ記録
        if self._logging_enabled:
            self.logger.add_record(log_record)
            count = self.logger.get_record_count()
            self.log_label.text = f'{count} rec'
            self.log_label.text_color = '#ef476f'

        self._schedule_update()

    def will_close(self):
        """ビューが閉じられる際の処理"""
        motion.stop_updates()
        location.stop_updates()

        # 気圧計停止
        self.barometer.stop()

        # スリープ防止解除（安全のため）
        set_sleep_disabled(False)

        # 記録中なら保存
        if self._logging_enabled and self.logger.get_record_count() > 0:
            filepath = self.logger.save()
            print(f'Log saved: {filepath}')


def main():
    """メイン関数"""
    view = SensorView()
    view.present('fullscreen')


if __name__ == '__main__':
    main()
