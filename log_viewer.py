# -*- coding: utf-8 -*-
"""
Sensor Logger Viewer - ログデータ可視化アプリ

PySide6 + PyQtGraph + 国土地理院地図 によるセンサーログの可視化ツール
"""

import sys
import json
import math
import numpy as np
import urllib.request
from pathlib import Path
from functools import lru_cache

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTabWidget, QFileDialog, QPushButton, QLabel,
    QGroupBox, QStatusBar, QComboBox, QTreeView, QHeaderView, QFileSystemModel
)
from PySide6.QtCore import Qt, QUrl, QDir
from PySide6.QtGui import QAction, QShortcut, QKeySequence
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebChannel import QWebChannel

import pyqtgraph as pg

# PyQtGraph設定
pg.setConfigOptions(antialias=True)


# 国土地理院地図HTML
MAP_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        body { margin: 0; padding: 0; }
        #map { width: 100%; height: 100vh; }
        .leaflet-control-attribution { font-size: 10px; }
        .info-box {
            background: rgba(255,255,255,0.9);
            padding: 8px 12px;
            border-radius: 4px;
            font-family: monospace;
            font-size: 12px;
        }
        .legend {
            background: rgba(255,255,255,0.9);
            padding: 10px;
            border-radius: 5px;
            font-family: sans-serif;
            font-size: 11px;
            line-height: 1.6;
        }
        .legend-item {
            display: flex;
            align-items: center;
            margin: 2px 0;
        }
        .legend-color {
            width: 20px;
            height: 4px;
            margin-right: 8px;
            border-radius: 2px;
        }
        .legend-color.dashed {
            background: repeating-linear-gradient(
                90deg,
                #9D4EDD,
                #9D4EDD 5px,
                transparent 5px,
                transparent 8px
            );
        }
        .legend-color.dotted {
            background: repeating-linear-gradient(
                90deg,
                #00CED1,
                #00CED1 3px,
                transparent 3px,
                transparent 6px
            );
        }
    </style>
</head>
<body>
    <div id="map"></div>
    <script>
        var map = L.map('map').setView([35.6812, 139.7671], 15);

        // タイルレイヤー定義
        var tileLayers = {
            gsi: L.tileLayer('https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png', {
                attribution: '<a href="https://maps.gsi.go.jp/development/ichiran.html">国土地理院</a>',
                maxZoom: 18
            }),
            gsi_std: L.tileLayer('https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png', {
                attribution: '<a href="https://maps.gsi.go.jp/development/ichiran.html">国土地理院</a>',
                maxZoom: 18
            }),
            gsi_photo: L.tileLayer('https://cyberjapandata.gsi.go.jp/xyz/seamlessphoto/{z}/{x}/{y}.jpg', {
                attribution: '<a href="https://maps.gsi.go.jp/development/ichiran.html">国土地理院</a>',
                maxZoom: 18
            }),
            osm: L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
                attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
                maxZoom: 19
            }),
            google_map: L.tileLayer('https://mt1.google.com/vt/lyrs=m&x={x}&y={y}&z={z}', {
                attribution: '&copy; Google Maps',
                maxZoom: 20
            }),
            google_satellite: L.tileLayer('https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}', {
                attribution: '&copy; Google Maps',
                maxZoom: 20
            }),
            google_hybrid: L.tileLayer('https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}', {
                attribution: '&copy; Google Maps',
                maxZoom: 20
            })
        };

        var currentTileLayer = tileLayers.gsi;
        currentTileLayer.addTo(map);

        function setMapType(mapType) {
            map.removeLayer(currentTileLayer);
            currentTileLayer = tileLayers[mapType] || tileLayers.gsi;
            currentTileLayer.addTo(map);
        }

        var gpsLayers = [];
        var drTrack = null;
        var insTrack = null;
        var startMarker = null;
        var endMarker = null;
        var insEndMarker = null;
        var currentMarker = null;
        var legendControl = null;

        // GPS精度による色分け
        var accuracyColors = {
            excellent: '#06d6a0',  // 緑 (< 5m)
            good: '#118ab2',       // 青 (< 15m)
            fair: '#ffd166',       // 黄 (< 30m)
            poor: '#f77f00',       // オレンジ (< 100m)
            very_poor: '#ef476f'   // 赤 (>= 100m)
        };

        // DR軌跡の色（紫/マゼンタ系）
        var drColor = '#9D4EDD';

        // INS軌跡の色（シアン系）
        var insColor = '#00CED1';

        function getAccuracyColor(accuracy) {
            if (accuracy < 5) return accuracyColors.excellent;
            if (accuracy < 15) return accuracyColors.good;
            if (accuracy < 30) return accuracyColors.fair;
            if (accuracy < 100) return accuracyColors.poor;
            return accuracyColors.very_poor;
        }

        function clearMap() {
            gpsLayers.forEach(function(layer) {
                map.removeLayer(layer);
            });
            gpsLayers = [];
            if (drTrack) map.removeLayer(drTrack);
            if (insTrack) map.removeLayer(insTrack);
            if (startMarker) map.removeLayer(startMarker);
            if (endMarker) map.removeLayer(endMarker);
            if (insEndMarker) map.removeLayer(insEndMarker);
            if (currentMarker) map.removeLayer(currentMarker);
            if (legendControl) map.removeControl(legendControl);
            drTrack = null;
            insTrack = null;
            startMarker = null;
            endMarker = null;
            insEndMarker = null;
            currentMarker = null;
            legendControl = null;
        }

        function setGPSTrackWithAccuracy(gpsData, drCoords, insCoords) {
            clearMap();

            if (gpsData.length === 0) return;

            var allCoords = [];

            // GPS軌跡を精度ごとにセグメント分けして描画
            var currentColor = null;
            var currentSegment = [];

            for (var i = 0; i < gpsData.length; i++) {
                var point = gpsData[i];
                var coord = [point.lat, point.lon];
                var color = getAccuracyColor(point.accuracy);
                allCoords.push(coord);

                if (currentColor === null) {
                    currentColor = color;
                    currentSegment.push(coord);
                } else if (color === currentColor) {
                    currentSegment.push(coord);
                } else {
                    // 色が変わった: 現在のセグメントを描画
                    if (currentSegment.length >= 2) {
                        var line = L.polyline(currentSegment, {
                            color: currentColor,
                            weight: 6,
                            opacity: 0.9,
                            lineCap: 'round',
                            lineJoin: 'round'
                        }).addTo(map);
                        gpsLayers.push(line);
                    }
                    // 新しいセグメント開始（前のポイントを含める）
                    currentSegment = [currentSegment[currentSegment.length - 1], coord];
                    currentColor = color;
                }
            }

            // 最後のセグメントを描画
            if (currentSegment.length >= 2) {
                var line = L.polyline(currentSegment, {
                    color: currentColor,
                    weight: 6,
                    opacity: 0.9,
                    lineCap: 'round',
                    lineJoin: 'round'
                }).addTo(map);
                gpsLayers.push(line);
            }

            // 開始点（白枠付き緑）
            if (allCoords.length > 0) {
                startMarker = L.circleMarker(allCoords[0], {
                    radius: 10,
                    fillColor: '#06d6a0',
                    color: '#fff',
                    weight: 3,
                    fillOpacity: 1
                }).addTo(map).bindPopup('Start');

                // 終了点（白枠付き赤）
                endMarker = L.circleMarker(allCoords[allCoords.length - 1], {
                    radius: 10,
                    fillColor: '#ef476f',
                    color: '#fff',
                    weight: 3,
                    fillOpacity: 1
                }).addTo(map).bindPopup('GPS End');
            }

            // INS軌跡（シアン、点線、太め）- センサーのみで計算
            if (insCoords && insCoords.length > 0) {
                insTrack = L.polyline(insCoords, {
                    color: insColor,
                    weight: 4,
                    opacity: 0.8,
                    dashArray: '4, 4',
                    lineCap: 'round',
                    lineJoin: 'round'
                }).addTo(map);

                // INS終了点マーカー
                insEndMarker = L.circleMarker(insCoords[insCoords.length - 1], {
                    radius: 8,
                    fillColor: insColor,
                    color: '#fff',
                    weight: 2,
                    fillOpacity: 1
                }).addTo(map).bindPopup('INS End');

                insCoords.forEach(function(c) {
                    allCoords.push(c);
                });
            }

            // DR軌跡（紫、破線、太め）- GPS途絶時のみ
            if (drCoords && drCoords.length > 0) {
                drTrack = L.polyline(drCoords, {
                    color: drColor,
                    weight: 5,
                    opacity: 0.9,
                    dashArray: '12, 6',
                    lineCap: 'round',
                    lineJoin: 'round'
                }).addTo(map);

                drCoords.forEach(function(c) {
                    allCoords.push(c);
                });
            }

            // 凡例を追加
            legendControl = L.control({position: 'bottomright'});
            legendControl.onAdd = function(map) {
                var div = L.DomUtil.create('div', 'legend');
                div.innerHTML = '<strong>Track Types</strong><br>' +
                    '<div class="legend-item"><div class="legend-color" style="background:#06d6a0"></div>GPS Excellent (&lt;5m)</div>' +
                    '<div class="legend-item"><div class="legend-color" style="background:#118ab2"></div>GPS Good (&lt;15m)</div>' +
                    '<div class="legend-item"><div class="legend-color" style="background:#ffd166"></div>GPS Fair (&lt;30m)</div>' +
                    '<div class="legend-item"><div class="legend-color" style="background:#f77f00"></div>GPS Poor (&lt;100m)</div>' +
                    '<div class="legend-item"><div class="legend-color" style="background:#ef476f"></div>GPS Very Poor</div>' +
                    '<div class="legend-item"><div class="legend-color dotted"></div>GPS/INS Fusion</div>' +
                    '<div class="legend-item"><div class="legend-color dashed"></div>DR (GPS Lost)</div>';
                return div;
            };
            legendControl.addTo(map);

            // 全体が見えるようにフィット
            if (allCoords.length > 0) {
                map.fitBounds(L.latLngBounds(allCoords), {padding: [30, 30]});
            }
        }

        // 後方互換性のため古い関数も残す
        function setGPSTrack(coords, drCoords, insCoords) {
            // 精度情報がない場合は全て青で描画
            var gpsData = coords.map(function(c) {
                return {lat: c[0], lon: c[1], accuracy: 10};
            });
            setGPSTrackWithAccuracy(gpsData, drCoords, insCoords || []);
        }

        function setCurrentPosition(lat, lon, isDR) {
            if (currentMarker) map.removeLayer(currentMarker);

            var color = isDR ? drColor : '#007AFF';
            currentMarker = L.circleMarker([lat, lon], {
                radius: 12,
                fillColor: color,
                color: '#fff',
                weight: 3,
                fillOpacity: 1
            }).addTo(map);
        }

        function panTo(lat, lon) {
            map.panTo([lat, lon]);
        }
    </script>
</body>
</html>
'''

# 統合航跡表示用HTML
INTEGRATED_MAP_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        body { margin: 0; padding: 0; }
        #map { width: 100%; height: 100vh; }
        .leaflet-control-attribution { font-size: 10px; }
        .legend {
            background: rgba(255,255,255,0.95);
            padding: 12px;
            border-radius: 5px;
            font-family: sans-serif;
            font-size: 11px;
            line-height: 1.8;
            box-shadow: 0 2px 6px rgba(0,0,0,0.2);
        }
        .legend-title {
            font-weight: bold;
            margin-bottom: 8px;
            border-bottom: 1px solid #ccc;
            padding-bottom: 4px;
        }
        .legend-item {
            display: flex;
            align-items: center;
            margin: 3px 0;
        }
        .legend-color {
            width: 24px;
            height: 4px;
            margin-right: 8px;
            border-radius: 2px;
        }
        .stats-box {
            background: rgba(255,255,255,0.95);
            padding: 12px;
            border-radius: 5px;
            font-family: monospace;
            font-size: 11px;
            line-height: 1.6;
            box-shadow: 0 2px 6px rgba(0,0,0,0.2);
        }
        .stats-title {
            font-weight: bold;
            margin-bottom: 8px;
            font-family: sans-serif;
        }
    </style>
</head>
<body>
    <div id="map"></div>
    <script>
        var map = L.map('map').setView([35.6812, 139.7671], 15);

        // タイルレイヤー定義
        var tileLayers = {
            gsi: L.tileLayer('https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png', {
                attribution: '<a href="https://maps.gsi.go.jp/development/ichiran.html">国土地理院</a>',
                maxZoom: 18
            }),
            gsi_std: L.tileLayer('https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png', {
                attribution: '<a href="https://maps.gsi.go.jp/development/ichiran.html">国土地理院</a>',
                maxZoom: 18
            }),
            gsi_photo: L.tileLayer('https://cyberjapandata.gsi.go.jp/xyz/seamlessphoto/{z}/{x}/{y}.jpg', {
                attribution: '<a href="https://maps.gsi.go.jp/development/ichiran.html">国土地理院</a>',
                maxZoom: 18
            }),
            osm: L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
                attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
                maxZoom: 19
            }),
            google_map: L.tileLayer('https://mt1.google.com/vt/lyrs=m&x={x}&y={y}&z={z}', {
                attribution: '&copy; Google Maps',
                maxZoom: 20
            }),
            google_satellite: L.tileLayer('https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}', {
                attribution: '&copy; Google Maps',
                maxZoom: 20
            }),
            google_hybrid: L.tileLayer('https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}', {
                attribution: '&copy; Google Maps',
                maxZoom: 20
            })
        };

        var currentTileLayer = tileLayers.gsi;
        currentTileLayer.addTo(map);

        function setMapType(mapType) {
            map.removeLayer(currentTileLayer);
            currentTileLayer = tileLayers[mapType] || tileLayers.gsi;
            currentTileLayer.addTo(map);
        }

        // 航跡タイプ別の色
        var trackColors = {
            gps_excellent: '#06d6a0',  // GPS精度良好: 緑
            gps_good: '#118ab2',       // GPS精度普通: 青
            gps_fair: '#ffd166',       // GPS精度やや悪: 黄
            fused: '#00CED1',          // GPS/INS融合: シアン
            memory: '#FF00FF',         // メモリートラック: マゼンタ
            ins: '#9D4EDD'             // INSのみ: 紫
        };

        var trackLayers = [];
        var startMarker = null;
        var endMarker = null;
        var legendControl = null;
        var statsControl = null;

        function clearTracks() {
            trackLayers.forEach(function(layer) {
                map.removeLayer(layer);
            });
            trackLayers = [];
            if (startMarker) { map.removeLayer(startMarker); startMarker = null; }
            if (endMarker) { map.removeLayer(endMarker); endMarker = null; }
            if (legendControl) { map.removeControl(legendControl); legendControl = null; }
            if (statsControl) { map.removeControl(statsControl); statsControl = null; }
        }

        function addTrackSegment(coords, trackType) {
            if (coords.length < 2) return;

            var color = trackColors[trackType] || '#888888';
            var dashArray = null;
            var weight = 5;

            // タイプ別の線種
            if (trackType === 'memory') {
                dashArray = '8, 4';
                weight = 4;
            } else if (trackType === 'ins') {
                dashArray = '4, 4';
                weight = 3;
            } else if (trackType === 'fused') {
                dashArray = '2, 4';
                weight = 4;
            }

            var polyline = L.polyline(coords, {
                color: color,
                weight: weight,
                opacity: 0.9,
                dashArray: dashArray
            }).addTo(map);

            trackLayers.push(polyline);
        }

        function setMarkers(startLat, startLon, endLat, endLon) {
            // 開始マーカー（大きめ）
            startMarker = L.circleMarker([startLat, startLon], {
                radius: 12,
                fillColor: '#00ff00',
                color: '#ffffff',
                weight: 3,
                fillOpacity: 1
            }).addTo(map).bindPopup('Start');

            // 終了マーカー（大きめ）
            endMarker = L.circleMarker([endLat, endLon], {
                radius: 12,
                fillColor: '#ff0000',
                color: '#ffffff',
                weight: 3,
                fillOpacity: 1
            }).addTo(map).bindPopup('End');
        }

        function fitBounds(coords) {
            if (coords.length > 0) {
                var bounds = L.latLngBounds(coords);
                map.fitBounds(bounds, { padding: [30, 30] });
            }
        }

        function addLegend(stats) {
            legendControl = L.control({ position: 'topright' });
            legendControl.onAdd = function(map) {
                var div = L.DomUtil.create('div', 'legend');
                div.innerHTML = '<div class="legend-title">統合航跡 凡例</div>' +
                    '<div class="legend-item"><div class="legend-color" style="background:' + trackColors.gps_excellent + '"></div>GPS (精度 &lt;5m)</div>' +
                    '<div class="legend-item"><div class="legend-color" style="background:' + trackColors.gps_good + '"></div>GPS (精度 &lt;15m)</div>' +
                    '<div class="legend-item"><div class="legend-color" style="background:' + trackColors.gps_fair + '"></div>GPS (精度 &lt;30m)</div>' +
                    '<div class="legend-item"><div class="legend-color" style="background:' + trackColors.fused + ';background:repeating-linear-gradient(90deg,' + trackColors.fused + ',' + trackColors.fused + ' 3px,transparent 3px,transparent 6px)"></div>GPS/INS Fusion</div>' +
                    '<div class="legend-item"><div class="legend-color" style="background:repeating-linear-gradient(90deg,' + trackColors.memory + ',' + trackColors.memory + ' 8px,transparent 8px,transparent 12px)"></div>Memory Track</div>' +
                    '<div class="legend-item"><div class="legend-color" style="background:repeating-linear-gradient(90deg,' + trackColors.ins + ',' + trackColors.ins + ' 4px,transparent 4px,transparent 8px)"></div>INS Only</div>';
                return div;
            };
            legendControl.addTo(map);
        }

        function addStats(stats) {
            statsControl = L.control({ position: 'bottomright' });
            statsControl.onAdd = function(map) {
                var div = L.DomUtil.create('div', 'stats-box');
                div.innerHTML = '<div class="stats-title">航跡統計</div>' +
                    '<div>総距離: ' + stats.total_distance.toFixed(1) + ' m</div>' +
                    '<div>GPS区間: ' + stats.gps_ratio.toFixed(1) + '%</div>' +
                    '<div>Fusion区間: ' + stats.fusion_ratio.toFixed(1) + '%</div>' +
                    '<div>Memory区間: ' + stats.memory_ratio.toFixed(1) + '%</div>' +
                    '<div>平均精度: ' + stats.avg_accuracy.toFixed(1) + ' m</div>';
                return div;
            };
            statsControl.addTo(map);
        }

        // 区間マーカー（Region選択用）
        var regionStartMarker = null;
        var regionEndMarker = null;

        function setRegionMarkers(startLat, startLon, endLat, endLon) {
            // 既存のマーカーを削除
            if (regionStartMarker) { map.removeLayer(regionStartMarker); }
            if (regionEndMarker) { map.removeLayer(regionEndMarker); }

            // 区間開始マーカー（ライムグリーン、元の始点より小さめ）
            regionStartMarker = L.circleMarker([startLat, startLon], {
                radius: 8,
                fillColor: '#32CD32',
                color: '#ffffff',
                weight: 2,
                fillOpacity: 0.9
            }).addTo(map).bindPopup('区間開始');

            // 区間終了マーカー（クリムゾン、元の終点より小さめ）
            regionEndMarker = L.circleMarker([endLat, endLon], {
                radius: 8,
                fillColor: '#DC143C',
                color: '#ffffff',
                weight: 2,
                fillOpacity: 0.9
            }).addTo(map).bindPopup('区間終了');
        }

        function clearRegionMarkers() {
            if (regionStartMarker) { map.removeLayer(regionStartMarker); regionStartMarker = null; }
            if (regionEndMarker) { map.removeLayer(regionEndMarker); regionEndMarker = null; }
        }
    </script>
</body>
</html>
'''


class GSIElevationAPI:
    """国土地理院標高タイルAPIクラス"""

    # タイルのベースURL
    DEM_URLS = [
        'https://cyberjapandata.gsi.go.jp/xyz/dem5a_png/{z}/{x}/{y}.png',  # 5mメッシュ（航空レーザー）
        'https://cyberjapandata.gsi.go.jp/xyz/dem5b_png/{z}/{x}/{y}.png',  # 5mメッシュ（写真測量）
        'https://cyberjapandata.gsi.go.jp/xyz/dem_png/{z}/{x}/{y}.png',    # 10mメッシュ
    ]

    def __init__(self, zoom=15):
        self.zoom = zoom
        self._cache = {}

    def _lat_lon_to_tile(self, lat, lon, zoom):
        """緯度経度をタイル座標に変換"""
        n = 2 ** zoom
        x = int((lon + 180.0) / 360.0 * n)
        lat_rad = math.radians(lat)
        y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
        return x, y

    def _lat_lon_to_pixel(self, lat, lon, zoom):
        """緯度経度をタイル内ピクセル座標に変換"""
        n = 2 ** zoom
        x_tile = (lon + 180.0) / 360.0 * n
        lat_rad = math.radians(lat)
        y_tile = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n

        # タイル内のピクセル位置（256x256）
        px = int((x_tile - int(x_tile)) * 256)
        py = int((y_tile - int(y_tile)) * 256)
        return px, py

    @lru_cache(maxsize=100)
    def _fetch_tile(self, x, y, zoom):
        """タイルを取得してキャッシュ"""
        for base_url in self.DEM_URLS:
            url = base_url.format(z=zoom, x=x, y=y)
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'SensorLogViewer/1.0'})
                with urllib.request.urlopen(req, timeout=5) as response:
                    from PIL import Image
                    import io
                    img_data = response.read()
                    img = Image.open(io.BytesIO(img_data))
                    return np.array(img)
            except Exception:
                continue
        return None

    def get_elevation(self, lat, lon):
        """指定座標の標高を取得"""
        x, y = self._lat_lon_to_tile(lat, lon, self.zoom)
        px, py = self._lat_lon_to_pixel(lat, lon, self.zoom)

        tile = self._fetch_tile(x, y, self.zoom)
        if tile is None:
            return None

        try:
            # PNGタイルから標高を計算
            # 国土地理院PNG標高タイル仕様:
            # x = 2^16 * R + 2^8 * G + B
            # x < 2^23: h = x * 0.01
            # x >= 2^23: h = (x - 2^24) * 0.01
            if len(tile.shape) >= 3:
                r = int(tile[py, px, 0])
                g = int(tile[py, px, 1])
                b = int(tile[py, px, 2])

                # 無効値チェック（128, 0, 0は海など）
                if r == 128 and g == 0 and b == 0:
                    return None

                # 標高計算
                x = r * 65536 + g * 256 + b
                if x >= 8388608:  # 2^23 (負の値)
                    x = x - 16777216  # 2^24
                h = x * 0.01

                return h
        except Exception:
            pass

        return None

    def get_elevation_profile(self, coords, sample_interval=10):
        """
        経路に沿った標高プロファイルを取得

        coords: [(lat, lon), ...] 座標リスト
        sample_interval: サンプリング間隔（インデックス）

        returns: [(distance, elevation), ...] 距離と標高のリスト
        """
        profile = []
        total_distance = 0.0
        prev_lat, prev_lon = None, None

        for i, (lat, lon) in enumerate(coords):
            if i % sample_interval != 0 and i != len(coords) - 1:
                # 距離は累積
                if prev_lat is not None:
                    dist = self._haversine(prev_lat, prev_lon, lat, lon)
                    total_distance += dist
                prev_lat, prev_lon = lat, lon
                continue

            # 距離計算
            if prev_lat is not None:
                dist = self._haversine(prev_lat, prev_lon, lat, lon)
                total_distance += dist

            # 標高取得
            elev = self.get_elevation(lat, lon)
            if elev is not None:
                profile.append((total_distance, elev))

            prev_lat, prev_lon = lat, lon

        return profile

    def _haversine(self, lat1, lon1, lat2, lon2):
        """2点間の距離を計算（メートル）"""
        R = 6378137.0
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)

        a = math.sin(dlat/2)**2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon/2)**2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

        return R * c


class AltitudeFusion:
    """高度融合クラス（GPS + 気圧計 + 加速度Z軸）"""

    def __init__(self):
        self.gps_altitude = None      # GPS基準高度
        self.baro_reference = None    # 気圧計基準値
        self.fused_altitude = None    # 融合高度
        self.vertical_velocity = 0.0  # 垂直速度

    def reset(self):
        """状態をリセット"""
        self.gps_altitude = None
        self.baro_reference = None
        self.fused_altitude = None
        self.vertical_velocity = 0.0

    def update(self, gps_alt, gps_v_acc, baro_relative, accel_z, dt):
        """
        高度を更新

        gps_alt: GPS高度 (m)
        gps_v_acc: GPS垂直精度 (m)、Noneまたは負値は無効
        baro_relative: 気圧計相対高度 (m)
        accel_z: Z軸加速度 (G)
        dt: 時間間隔 (s)

        returns: 融合高度 (m)
        """
        # GPS高度で初期化
        if self.gps_altitude is None:
            if gps_alt is not None and gps_alt != 0:
                self.gps_altitude = gps_alt
                self.baro_reference = baro_relative if baro_relative is not None else 0
                self.fused_altitude = gps_alt
                return self.fused_altitude
            return None

        # 気圧計による高度変化（主センサー）
        if baro_relative is not None and self.baro_reference is not None:
            delta_baro = baro_relative - self.baro_reference
            self.fused_altitude = self.gps_altitude + delta_baro
        elif gps_alt is not None and gps_alt != 0:
            # 気圧計がない場合はGPS高度で更新
            self.fused_altitude = gps_alt

        # 加速度Z軸による補助（急激な変化の検出）
        if accel_z is not None and dt > 0:
            # 垂直加速度から速度を推定（簡易的）
            # 重力を除いた加速度なので、直接使用可能
            self.vertical_velocity += accel_z * 9.81 * dt
            self.vertical_velocity *= 0.95  # 減衰

        # GPS精度が良い時は基準を補正
        if gps_alt is not None and gps_v_acc is not None and gps_v_acc > 0 and gps_v_acc < 15:
            weight = 0.3  # GPS高度の重みを上げる
            self.gps_altitude = (1 - weight) * self.gps_altitude + weight * gps_alt
            if baro_relative is not None:
                self.baro_reference = baro_relative

        return self.fused_altitude

    def get_vertical_velocity(self):
        """垂直速度を取得 (m/s)"""
        return self.vertical_velocity


class GPSINSFusion:
    """GPS/INS融合による位置推定クラス（簡易Kalmanフィルタ + メモリートラック）"""

    EARTH_RADIUS = 6378137.0  # 地球半径 [m]

    # メモリートラック設定
    ACCURACY_THRESHOLD_GOOD = 15.0   # これ以下なら速度を記憶
    ACCURACY_THRESHOLD_DEGRADE = 30.0  # これ以上でメモリートラック発動
    MEMORY_VELOCITY_DECAY = 0.98     # メモリー速度の減衰率（per update）
    MEMORY_MAX_DURATION = 60.0       # メモリートラック最大持続時間（秒）

    def __init__(self, start_lat, start_lon):
        """開始位置を設定"""
        self.current_lat = start_lat
        self.current_lon = start_lon

        # 速度（北方向、東方向）[m/s]
        self.velocity_north = 0.0
        self.velocity_east = 0.0

        # 推定誤差共分散（簡易版）
        self.position_uncertainty = 10.0  # m
        self.velocity_uncertainty = 1.0   # m/s

        # GPS補正用
        self.last_gps_lat = start_lat
        self.last_gps_lon = start_lon
        self.last_gps_time = None

        # メモリートラック用
        self.memory_velocity_north = 0.0
        self.memory_velocity_east = 0.0
        self.memory_heading = 0.0
        self.memory_speed = 0.0
        self.is_memory_mode = False
        self.memory_mode_start_time = None
        self.last_good_gps_time = None

        # 軌跡（タイプ付き: 'gps', 'ins', 'fused', 'memory'）
        self.track = [(start_lat, start_lon, 'gps')]

    def update_gps(self, lat, lon, speed, course, accuracy, timestamp):
        """GPS観測で状態を更新（測定更新）"""
        # GPS精度が良好な場合：速度をメモリに記憶
        if accuracy >= 0 and accuracy < self.ACCURACY_THRESHOLD_GOOD:
            if course >= 0 and speed > 0.3:
                course_rad = np.radians(course)
                self.memory_velocity_north = speed * np.cos(course_rad)
                self.memory_velocity_east = speed * np.sin(course_rad)
                self.memory_heading = course_rad
                self.memory_speed = speed
            self.last_good_gps_time = timestamp

            # メモリーモード解除
            if self.is_memory_mode:
                self.is_memory_mode = False
                self.memory_mode_start_time = None

        # GPS精度が悪化した場合：メモリートラックモードへ
        if accuracy < 0 or accuracy >= self.ACCURACY_THRESHOLD_DEGRADE:
            if not self.is_memory_mode and self.memory_speed > 0.3:
                self.is_memory_mode = True
                self.memory_mode_start_time = timestamp
            return  # GPS更新をスキップ

        # 通常のGPS更新処理
        # GPS精度に基づく信頼度（Kalmanゲイン的）
        gps_weight = 1.0 / (1.0 + accuracy / 10.0)  # 0〜1

        # 位置を補正
        self.current_lat = (1 - gps_weight) * self.current_lat + gps_weight * lat
        self.current_lon = (1 - gps_weight) * self.current_lon + gps_weight * lon

        # 速度を補正（GPS速度が有効な場合）
        if speed >= 0 and course >= 0:
            course_rad = np.radians(course)
            gps_vel_north = speed * np.cos(course_rad)
            gps_vel_east = speed * np.sin(course_rad)

            vel_weight = gps_weight * 0.8  # 速度は位置より信頼度低め
            self.velocity_north = (1 - vel_weight) * self.velocity_north + vel_weight * gps_vel_north
            self.velocity_east = (1 - vel_weight) * self.velocity_east + vel_weight * gps_vel_east

        # 不確実性を減少
        self.position_uncertainty = accuracy
        self.velocity_uncertainty = accuracy / 10.0

        self.last_gps_lat = lat
        self.last_gps_lon = lon
        self.last_gps_time = timestamp

        self.track.append((self.current_lat, self.current_lon, 'fused'))

    def update_ins(self, user_accel, attitude, dt, timestamp=None):
        """
        センサーデータで位置を更新

        user_accel: (ax, ay, az) ユーザー加速度 [G]
        attitude: (roll, pitch, yaw) 姿勢 [rad]
        dt: 時間間隔 [s]
        timestamp: タイムスタンプ（メモリートラック用）
        """
        if dt <= 0:
            return

        use_memory_track = False
        track_type = 'ins'

        # メモリートラックモードの判定
        if self.is_memory_mode and self.memory_mode_start_time and timestamp:
            memory_elapsed = timestamp - self.memory_mode_start_time
            if memory_elapsed < self.MEMORY_MAX_DURATION and self.memory_speed > 0.3:
                use_memory_track = True

        if use_memory_track:
            # === メモリートラックモード ===
            # 記憶した速度で等速直線運動を仮定
            track_type = 'memory'

            # ジャイロで方位変化のみ検出（旋回対応）
            if attitude is not None:
                cur_roll, cur_pitch, yaw = attitude
                # 方位変化を検出してメモリ方位に適用
                heading = 3*np.pi/2 - yaw
                # 簡易的に現在のyawから方位を更新
                self.memory_heading = heading

            # メモリ速度を減衰（時間経過で信頼度低下）
            self.memory_velocity_north *= self.MEMORY_VELOCITY_DECAY
            self.memory_velocity_east *= self.MEMORY_VELOCITY_DECAY
            self.memory_speed *= self.MEMORY_VELOCITY_DECAY

            # 方位変化を反映した速度ベクトル
            vel_north = self.memory_speed * np.cos(self.memory_heading)
            vel_east = self.memory_speed * np.sin(self.memory_heading)

            # 位置の更新（メモリ速度使用）
            delta_lat = (vel_north * dt) / self.EARTH_RADIUS
            delta_lon = (vel_east * dt) / (
                self.EARTH_RADIUS * np.cos(np.radians(self.current_lat))
            )

            self.current_lat += np.degrees(delta_lat)
            self.current_lon += np.degrees(delta_lon)

        else:
            # === 通常INSモード ===
            # 現在の方位を計算
            heading = 0.0
            cur_pitch = 0.0
            cur_roll = 0.0

            if attitude is not None:
                cur_roll, cur_pitch, yaw = attitude
                heading = 3*np.pi/2 - yaw

            # 加速度を世界座標系に変換
            if user_accel is not None:
                ax, ay, az = user_accel

                cos_pitch = np.cos(cur_pitch)
                sin_pitch = np.sin(cur_pitch)
                cos_roll = np.cos(cur_roll)

                ay_corrected = ay * cos_pitch - az * sin_pitch
                ax_corrected = ax * cos_roll

                accel_forward = ay_corrected
                accel_right = ax_corrected

                accel_north = (accel_forward * np.cos(heading)
                              - accel_right * np.sin(heading))
                accel_east = (accel_forward * np.sin(heading)
                             + accel_right * np.cos(heading))

                accel_mag = np.sqrt(ax*ax + ay*ay + az*az)

                if abs(accel_mag) < 0.03:
                    self.velocity_north *= 0.8
                    self.velocity_east *= 0.8
                else:
                    move_threshold = 0.05
                    if abs(accel_forward) > move_threshold or abs(accel_right) > move_threshold:
                        self.velocity_north += accel_north * 9.81 * dt
                        self.velocity_east += accel_east * 9.81 * dt

            # 速度減衰
            decay = 0.99
            self.velocity_north *= decay
            self.velocity_east *= decay

            # 速度上限
            max_speed = 10.0
            speed = np.sqrt(self.velocity_north**2 + self.velocity_east**2)
            if speed > max_speed:
                scale = max_speed / speed
                self.velocity_north *= scale
                self.velocity_east *= scale

            # 位置を更新
            delta_lat = (self.velocity_north * dt) / self.EARTH_RADIUS
            delta_lon = (self.velocity_east * dt) / (
                self.EARTH_RADIUS * np.cos(np.radians(self.current_lat))
            )

            self.current_lat += np.degrees(delta_lat)
            self.current_lon += np.degrees(delta_lon)

        # 不確実性を増加
        self.position_uncertainty += 0.5 * dt
        self.velocity_uncertainty += 0.1 * dt

        self.track.append((self.current_lat, self.current_lon, track_type))

    def get_track(self):
        """軌跡を取得（座標のみ）"""
        return [(lat, lon) for lat, lon, _ in self.track]

    def get_track_with_type(self):
        """軌跡をタイプ付きで取得"""
        return self.track

    def get_current_position(self):
        """現在位置を取得"""
        return (self.current_lat, self.current_lon)

    def get_speed(self):
        """現在速度を取得 [m/s]"""
        return np.sqrt(self.velocity_north**2 + self.velocity_east**2)

    def get_uncertainty(self):
        """現在の位置不確実性を取得 [m]"""
        return self.position_uncertainty


# 後方互換性のためのエイリアス
INSCalculator = GPSINSFusion


class SensorLogViewer(QMainWindow):
    """センサーログビューアのメインウィンドウ"""

    def __init__(self, folder_path=None):
        super().__init__()
        self.setWindowTitle('Sensor Logger Viewer')
        self.setGeometry(100, 100, 1600, 900)

        self.log_data = None
        self.records = []
        self.time_array = None
        self._folder_path = folder_path

        self._setup_ui()
        self._setup_menu()

    def _setup_menu(self):
        """メニューバーの設定"""
        menubar = self.menuBar()

        file_menu = menubar.addMenu('File')

        open_action = QAction('Open...', self)
        open_action.setShortcut('Ctrl+O')
        open_action.triggered.connect(self._open_file)
        file_menu.addAction(open_action)

        file_menu.addSeparator()

        quit_action = QAction('Quit', self)
        quit_action.setShortcut('Ctrl+Q')
        quit_action.triggered.connect(self._safe_quit)
        file_menu.addAction(quit_action)

    def _safe_quit(self):
        """安全に終了"""
        self.close()

    def closeEvent(self, event):
        """ウィンドウを閉じる際のクリーンアップ"""
        try:
            # WebViewをクリーンアップ
            if hasattr(self, 'gps_map_view'):
                self.gps_map_view.setUrl(QUrl('about:blank'))
                self.gps_map_view.deleteLater()
            if hasattr(self, 'dr_map_view'):
                self.dr_map_view.setUrl(QUrl('about:blank'))
                self.dr_map_view.deleteLater()
            if hasattr(self, 'integrated_map_view'):
                self.integrated_map_view.setUrl(QUrl('about:blank'))
                self.integrated_map_view.deleteLater()
        except Exception as e:
            print(f'Cleanup error: {e}')

        event.accept()
        QApplication.quit()

    def _setup_file_tree(self, splitter, folder_path=None):
        """ファイルツリーのセットアップ"""
        # ファイルシステムモデル
        self.file_model = QFileSystemModel()
        self.file_model.setRootPath('')
        self.file_model.setNameFilters(['*.json'])
        self.file_model.setNameFilterDisables(False)

        # ツリービュー
        self.file_tree = QTreeView()
        self.file_tree.setModel(self.file_model)

        # フォルダパスを決定
        if folder_path and Path(folder_path).exists():
            root_path = Path(folder_path).resolve()
        else:
            # 省略時はカレントディレクトリ
            root_path = Path.cwd()

        self.file_tree.setRootIndex(self.file_model.index(str(root_path)))
        self._current_folder = root_path

        # 列の表示設定（ファイル名とサイズを表示）
        self.file_tree.setHeaderHidden(False)
        self.file_tree.hideColumn(2)  # Type列を非表示
        self.file_tree.hideColumn(3)  # Date Modified列を非表示

        # 列幅の調整
        self.file_tree.setColumnWidth(0, 200)  # Name
        self.file_tree.setColumnWidth(1, 80)   # Size

        # サイズ設定
        self.file_tree.setMinimumWidth(250)
        self.file_tree.setMaximumWidth(350)

        # シングルクリックでファイルを開く
        self.file_tree.clicked.connect(self._on_file_tree_clicked)

        splitter.addWidget(self.file_tree)

    def _on_file_tree_clicked(self, index):
        """ファイルツリーのクリックイベント"""
        file_path = self.file_model.filePath(index)
        if file_path.endswith('.json') and Path(file_path).is_file():
            self._load_file(file_path)

    def _setup_ui(self):
        """UIの設定"""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        main_layout = QVBoxLayout(central_widget)

        # ファイル名表示
        self.file_label = QLabel('No file loaded')
        main_layout.addWidget(self.file_label)

        # メインスプリッター（ファイルツリー | グラフ | 情報パネル）
        splitter = QSplitter(Qt.Horizontal)

        # 左側: ファイルツリー
        self._setup_file_tree(splitter, self._folder_path)

        # 中央: グラフタブ
        self.tab_widget = QTabWidget()

        # モーションセンサータブ
        motion_widget = QWidget()
        motion_layout = QVBoxLayout(motion_widget)

        self.gravity_plot = pg.PlotWidget(title='Gravity (G)')
        self.gravity_plot.addLegend()
        self.gravity_plot.showGrid(x=True, y=True)
        motion_layout.addWidget(self.gravity_plot)

        self.accel_plot = pg.PlotWidget(title='User Acceleration (G)')
        self.accel_plot.addLegend()
        self.accel_plot.showGrid(x=True, y=True)
        motion_layout.addWidget(self.accel_plot)

        self.tab_widget.addTab(motion_widget, 'Acceleration')

        # 姿勢タブ
        attitude_widget = QWidget()
        attitude_layout = QVBoxLayout(attitude_widget)

        self.attitude_plot = pg.PlotWidget(title='Attitude (degrees)')
        self.attitude_plot.addLegend()
        self.attitude_plot.showGrid(x=True, y=True)
        attitude_layout.addWidget(self.attitude_plot)

        self.gyro_plot = pg.PlotWidget(title='Gyroscope (rad/s)')
        self.gyro_plot.addLegend()
        self.gyro_plot.showGrid(x=True, y=True)
        attitude_layout.addWidget(self.gyro_plot)

        self.tab_widget.addTab(attitude_widget, 'Attitude')

        # 磁場タブ
        magnetic_widget = QWidget()
        magnetic_layout = QVBoxLayout(magnetic_widget)

        self.magnetic_plot = pg.PlotWidget(title='Magnetic Field (μT)')
        self.magnetic_plot.addLegend()
        self.magnetic_plot.showGrid(x=True, y=True)
        magnetic_layout.addWidget(self.magnetic_plot)

        self.tab_widget.addTab(magnetic_widget, 'Magnetic')

        # GPSタブ（国土地理院地図）
        gps_widget = QWidget()
        gps_layout = QVBoxLayout(gps_widget)

        # 地図選択コンボボックス
        gps_map_control = QHBoxLayout()
        gps_map_label = QLabel('Map Type:')
        gps_map_control.addWidget(gps_map_label)
        self.gps_map_combo = QComboBox()
        self._setup_map_combo(self.gps_map_combo)
        gps_map_control.addWidget(self.gps_map_combo)
        gps_map_control.addStretch()
        gps_layout.addLayout(gps_map_control)

        gps_splitter = QSplitter(Qt.Horizontal)

        # 左: 地図
        self.map_view = QWebEngineView()
        self.map_view.setHtml(MAP_HTML)
        self.gps_map_combo.currentIndexChanged.connect(
            lambda: self._change_map_type(self.map_view, self.gps_map_combo)
        )
        gps_splitter.addWidget(self.map_view)

        # 右: グラフ
        gps_graphs = QWidget()
        gps_graphs_layout = QVBoxLayout(gps_graphs)

        self.altitude_plot = pg.PlotWidget(title='Altitude (m)')
        self.altitude_plot.showGrid(x=True, y=True)
        gps_graphs_layout.addWidget(self.altitude_plot)

        self.speed_plot = pg.PlotWidget(title='Speed (m/s)')
        self.speed_plot.showGrid(x=True, y=True)
        gps_graphs_layout.addWidget(self.speed_plot)

        self.accuracy_plot = pg.PlotWidget(title='GPS Accuracy (m)')
        self.accuracy_plot.showGrid(x=True, y=True)
        gps_graphs_layout.addWidget(self.accuracy_plot)

        gps_splitter.addWidget(gps_graphs)
        gps_splitter.setSizes([700, 400])

        gps_layout.addWidget(gps_splitter)

        self.tab_widget.addTab(gps_widget, 'GPS')

        # デッドレコニングタブ
        dr_widget = QWidget()
        dr_layout = QVBoxLayout(dr_widget)

        # 地図選択コンボボックス
        dr_map_control = QHBoxLayout()
        dr_map_label = QLabel('Map Type:')
        dr_map_control.addWidget(dr_map_label)
        self.dr_map_combo = QComboBox()
        self._setup_map_combo(self.dr_map_combo)
        dr_map_control.addWidget(self.dr_map_combo)
        dr_map_control.addStretch()
        dr_layout.addLayout(dr_map_control)

        dr_splitter = QSplitter(Qt.Horizontal)

        # 左: 地図（DR比較用）
        self.dr_map_view = QWebEngineView()
        self.dr_map_view.setHtml(MAP_HTML)
        self.dr_map_combo.currentIndexChanged.connect(
            lambda: self._change_map_type(self.dr_map_view, self.dr_map_combo)
        )
        dr_splitter.addWidget(self.dr_map_view)

        # 右: グラフ
        dr_graphs = QWidget()
        dr_graphs_layout = QVBoxLayout(dr_graphs)

        self.dr_speed_plot = pg.PlotWidget(title='DR Speed (m/s)')
        self.dr_speed_plot.showGrid(x=True, y=True)
        self.dr_speed_plot.addLegend()
        dr_graphs_layout.addWidget(self.dr_speed_plot)

        self.dr_heading_plot = pg.PlotWidget(title='DR Heading (deg)')
        self.dr_heading_plot.showGrid(x=True, y=True)
        dr_graphs_layout.addWidget(self.dr_heading_plot)

        self.dr_error_plot = pg.PlotWidget(title='DR vs GPS Distance (m)')
        self.dr_error_plot.showGrid(x=True, y=True)
        dr_graphs_layout.addWidget(self.dr_error_plot)

        dr_splitter.addWidget(dr_graphs)
        dr_splitter.setSizes([700, 400])

        dr_layout.addWidget(dr_splitter)

        self.tab_widget.addTab(dr_widget, 'Dead Reckoning')

        # 統合航跡タブ
        integrated_widget = QWidget()
        integrated_layout = QVBoxLayout(integrated_widget)

        # 地図選択コンボボックス
        int_map_control = QHBoxLayout()
        int_map_label = QLabel('Map Type:')
        int_map_control.addWidget(int_map_label)
        self.integrated_map_combo = QComboBox()
        self._setup_map_combo(self.integrated_map_combo)
        int_map_control.addWidget(self.integrated_map_combo)
        int_map_control.addStretch()
        integrated_layout.addLayout(int_map_control)

        # 地図と断面図のスプリッター（縦分割）
        integrated_splitter = QSplitter(Qt.Vertical)

        # 統合航跡用の地図
        self.integrated_map_view = QWebEngineView()
        self.integrated_map_view.setHtml(INTEGRATED_MAP_HTML)
        self.integrated_map_combo.currentIndexChanged.connect(
            lambda: self._change_map_type(self.integrated_map_view, self.integrated_map_combo)
        )
        integrated_splitter.addWidget(self.integrated_map_view)

        # 標高断面図
        elevation_widget = QWidget()
        elevation_layout = QVBoxLayout(elevation_widget)
        elevation_layout.setContentsMargins(0, 0, 0, 0)

        self.elevation_plot = pg.PlotWidget(title='標高断面図')
        self.elevation_plot.showGrid(x=True, y=True)
        self.elevation_plot.setLabel('bottom', '距離', 'm')
        self.elevation_plot.setLabel('left', '標高', 'm')
        self.elevation_plot.addLegend()
        elevation_layout.addWidget(self.elevation_plot)

        integrated_splitter.addWidget(elevation_widget)
        integrated_splitter.setSizes([500, 200])  # 地図:断面図 = 5:2

        integrated_layout.addWidget(integrated_splitter)

        self.tab_widget.addTab(integrated_widget, 'Integrated Track')

        splitter.addWidget(self.tab_widget)

        # 右側: 情報パネル
        info_panel = QWidget()
        info_layout = QVBoxLayout(info_panel)

        # メタデータ
        meta_group = QGroupBox('Metadata')
        meta_layout = QVBoxLayout(meta_group)
        self.meta_label = QLabel('No data')
        self.meta_label.setWordWrap(True)
        meta_layout.addWidget(self.meta_label)
        info_layout.addWidget(meta_group)

        # 統計情報
        stats_group = QGroupBox('Statistics')
        stats_layout = QVBoxLayout(stats_group)
        self.stats_label = QLabel('No data')
        self.stats_label.setWordWrap(True)
        stats_layout.addWidget(self.stats_label)
        info_layout.addWidget(stats_group)

        # GPS情報
        gps_group = QGroupBox('GPS Summary')
        gps_info_layout = QVBoxLayout(gps_group)
        self.gps_info_label = QLabel('No data')
        self.gps_info_label.setWordWrap(True)
        gps_info_layout.addWidget(self.gps_info_label)
        info_layout.addWidget(gps_group)

        # DR情報
        dr_group = QGroupBox('Dead Reckoning')
        dr_info_layout = QVBoxLayout(dr_group)
        self.dr_info_label = QLabel('No data')
        self.dr_info_label.setWordWrap(True)
        dr_info_layout.addWidget(self.dr_info_label)
        info_layout.addWidget(dr_group)

        info_layout.addStretch()

        splitter.addWidget(info_panel)
        # ファイルツリー: 280, グラフ: 1000, 情報パネル: 300
        splitter.setSizes([280, 1000, 300])

        main_layout.addWidget(splitter)

        # ステータスバー
        self.statusBar = QStatusBar()
        self.setStatusBar(self.statusBar)

    def _open_file(self):
        """ファイルを開く"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, 'Open Sensor Log', '',
            'JSON Files (*.json);;All Files (*)'
        )

        if file_path:
            self._load_file(file_path)

    def _load_file(self, file_path):
        """ファイルを読み込む"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                self.log_data = json.load(f)

            self.records = self.log_data.get('records', [])

            if not self.records:
                self.statusBar.showMessage('No records found in file')
                return

            self.file_label.setText(Path(file_path).name)
            self._update_metadata()
            self._plot_data()

            self.statusBar.showMessage(f'Loaded {len(self.records)} records')

        except Exception as e:
            self.statusBar.showMessage(f'Error loading file: {e}')

    def _update_metadata(self):
        """メタデータを更新"""
        if not self.log_data:
            return

        metadata = self.log_data.get('metadata', {})
        record_count = self.log_data.get('record_count', 0)

        meta_text = f"""Session: {metadata.get('session_start', 'N/A')}
Device: {metadata.get('device', 'N/A')}
Version: {metadata.get('app_version', 'N/A')}
Interval: {metadata.get('update_interval_ms', 'N/A')}ms
Records: {record_count}"""

        self.meta_label.setText(meta_text)

        # 統計情報
        if self.records:
            first_time = self.records[0].get('timestamp', 0)
            last_time = self.records[-1].get('timestamp', 0)
            duration = last_time - first_time

            stats_text = f"""Duration: {duration:.1f}s ({duration/60:.1f}min)
Avg Rate: {len(self.records)/max(duration, 0.1):.1f}Hz
Start: {self.records[0].get('datetime', 'N/A')[:19]}
End: {self.records[-1].get('datetime', 'N/A')[:19]}"""

            self.stats_label.setText(stats_text)

    def _setup_map_combo(self, combo):
        """地図選択コンボボックスをセットアップ"""
        combo.addItem('国土地理院（淡色）', 'gsi')
        combo.addItem('国土地理院（標準）', 'gsi_std')
        combo.addItem('国土地理院（写真）', 'gsi_photo')
        combo.addItem('OpenStreetMap', 'osm')
        combo.addItem('Google Maps', 'google_map')
        combo.addItem('Google 衛星', 'google_satellite')
        combo.addItem('Google ハイブリッド', 'google_hybrid')

    def _change_map_type(self, map_view, combo):
        """地図タイプを変更"""
        map_type = combo.currentData()
        if map_type:
            js = f'setMapType("{map_type}");'
            map_view.page().runJavaScript(js)

    def _extract_sensor_data(self):
        """センサーデータを抽出"""
        n = len(self.records)

        # 時間配列
        self.time_array = np.zeros(n)

        # 重力
        self.gravity_x = np.zeros(n)
        self.gravity_y = np.zeros(n)
        self.gravity_z = np.zeros(n)

        # ユーザー加速度
        self.accel_x = np.zeros(n)
        self.accel_y = np.zeros(n)
        self.accel_z = np.zeros(n)

        # 姿勢
        self.roll = np.zeros(n)
        self.pitch = np.zeros(n)
        self.yaw = np.zeros(n)

        # ジャイロ
        self.gyro_x = np.zeros(n)
        self.gyro_y = np.zeros(n)
        self.gyro_z = np.zeros(n)

        # 磁場
        self.mag_x = np.zeros(n)
        self.mag_y = np.zeros(n)
        self.mag_z = np.zeros(n)

        # GPS
        self.gps_lat = []
        self.gps_lon = []
        self.gps_alt = []
        self.gps_speed = []
        self.gps_accuracy = []
        self.gps_time = []

        # デッドレコニング
        self.dr_lat = []
        self.dr_lon = []
        self.dr_speed = []
        self.dr_heading = []
        self.dr_time = []

        first_time = self.records[0].get('timestamp', 0)

        for i, rec in enumerate(self.records):
            t = rec.get('timestamp', 0) - first_time
            self.time_array[i] = t

            sensors = rec.get('sensors', {})

            # 重力
            gravity = sensors.get('gravity')
            if gravity:
                self.gravity_x[i] = gravity.get('x', 0)
                self.gravity_y[i] = gravity.get('y', 0)
                self.gravity_z[i] = gravity.get('z', 0)

            # ユーザー加速度
            accel = sensors.get('user_acceleration')
            if accel:
                self.accel_x[i] = accel.get('x', 0)
                self.accel_y[i] = accel.get('y', 0)
                self.accel_z[i] = accel.get('z', 0)

            # 姿勢
            attitude = sensors.get('attitude')
            if attitude:
                self.roll[i] = attitude.get('roll_deg', 0)
                self.pitch[i] = attitude.get('pitch_deg', 0)
                self.yaw[i] = attitude.get('yaw_deg', 0)

            # ジャイロ
            gyro = sensors.get('gyro_calculated')
            if gyro:
                self.gyro_x[i] = gyro.get('x', 0)
                self.gyro_y[i] = gyro.get('y', 0)
                self.gyro_z[i] = gyro.get('z', 0)

            # 磁場
            mag = sensors.get('magnetic_field')
            if mag:
                self.mag_x[i] = mag.get('x', 0)
                self.mag_y[i] = mag.get('y', 0)
                self.mag_z[i] = mag.get('z', 0)

            # GPS
            gps = rec.get('gps', {})
            raw = gps.get('raw')
            if raw and not gps.get('no_signal', True):
                self.gps_lat.append(raw.get('latitude', 0))
                self.gps_lon.append(raw.get('longitude', 0))
                self.gps_alt.append(raw.get('altitude', 0))
                self.gps_speed.append(raw.get('speed_clamped', 0))
                self.gps_accuracy.append(raw.get('horizontal_accuracy', 0))
                self.gps_time.append(t)

            # デッドレコニング
            dr = rec.get('dead_reckoning', {})
            if dr.get('active'):
                result = dr.get('result', {})
                if result:
                    self.dr_lat.append(result.get('latitude', 0))
                    self.dr_lon.append(result.get('longitude', 0))
                    self.dr_speed.append(result.get('speed', 0))
                    self.dr_heading.append(result.get('heading_deg', 0))
                    self.dr_time.append(t)

        # リストをnumpy配列に変換
        self.gps_lat = np.array(self.gps_lat)
        self.gps_lon = np.array(self.gps_lon)
        self.gps_alt = np.array(self.gps_alt)
        self.gps_speed = np.array(self.gps_speed)
        self.gps_accuracy = np.array(self.gps_accuracy)
        self.gps_time = np.array(self.gps_time)

        self.dr_lat = np.array(self.dr_lat)
        self.dr_lon = np.array(self.dr_lon)
        self.dr_speed = np.array(self.dr_speed)
        self.dr_heading = np.array(self.dr_heading)
        self.dr_time = np.array(self.dr_time)

        # INS（慣性航法）軌跡を計算
        self._calculate_ins_track()

    def _calculate_ins_track(self):
        """GPS/INS融合で軌跡を計算"""
        self.ins_lat = []
        self.ins_lon = []

        # 開始位置がない場合は計算しない
        if len(self.gps_lat) == 0:
            return

        start_lat = self.gps_lat[0]
        start_lon = self.gps_lon[0]

        # GPS/INS融合インスタンスを作成
        fusion = GPSINSFusion(start_lat, start_lon)

        # 全レコードを処理
        prev_time = None
        for i, rec in enumerate(self.records):
            t = rec.get('timestamp', 0)

            if prev_time is None:
                prev_time = t
                continue

            dt = t - prev_time
            prev_time = t

            # センサーデータを取得
            sensors = rec.get('sensors', {})

            # ユーザー加速度
            user_accel = None
            accel_data = sensors.get('user_acceleration')
            if accel_data:
                user_accel = (
                    accel_data.get('x', 0),
                    accel_data.get('y', 0),
                    accel_data.get('z', 0)
                )

            # 姿勢（ラジアン）
            attitude = None
            att_data = sensors.get('attitude')
            if att_data:
                attitude = (
                    att_data.get('roll_rad', 0),
                    att_data.get('pitch_rad', 0),
                    att_data.get('yaw_rad', 0)
                )

            # INS更新（予測ステップ）
            fusion.update_ins(user_accel, attitude, dt, timestamp=t)

            # GPS更新（測定ステップ）- 有効なGPSがあれば補正
            gps = rec.get('gps', {})
            raw = gps.get('raw')
            if raw and not gps.get('no_signal', True):
                lat = raw.get('latitude', 0)
                lon = raw.get('longitude', 0)
                speed = raw.get('speed_clamped', raw.get('speed', -1))
                course = raw.get('course', -1)
                accuracy = raw.get('horizontal_accuracy', 100)
                timestamp = raw.get('timestamp')

                fusion.update_gps(lat, lon, speed, course, accuracy, timestamp)

        # 軌跡を取得（間引いて保存）
        track = fusion.get_track()
        step = max(1, len(track) // 500)  # 最大500点に間引き
        for i in range(0, len(track), step):
            lat, lon = track[i]
            self.ins_lat.append(lat)
            self.ins_lon.append(lon)

        # 最後の点を確実に含める
        if len(track) > 0:
            last_lat, last_lon = track[-1]
            if len(self.ins_lat) == 0 or (self.ins_lat[-1] != last_lat or self.ins_lon[-1] != last_lon):
                self.ins_lat.append(last_lat)
                self.ins_lon.append(last_lon)

        self.ins_lat = np.array(self.ins_lat)
        self.ins_lon = np.array(self.ins_lon)

    def _plot_data(self):
        """データをプロット"""
        self._extract_sensor_data()

        # プロット用の鮮やかな色（ダークテーマ用）
        pen_x = pg.mkPen('#FF6B6B', width=2)  # 鮮やかな赤
        pen_y = pg.mkPen('#4ECB71', width=2)  # 鮮やかな緑
        pen_z = pg.mkPen('#4DABF7', width=2)  # 鮮やかな青

        # 重力プロット
        self.gravity_plot.clear()
        self.gravity_plot.addLegend()
        self.gravity_plot.plot(self.time_array, self.gravity_x,
                               pen=pen_x, name='X')
        self.gravity_plot.plot(self.time_array, self.gravity_y,
                               pen=pen_y, name='Y')
        self.gravity_plot.plot(self.time_array, self.gravity_z,
                               pen=pen_z, name='Z')
        self.gravity_plot.setLabel('bottom', 'Time', 's')

        # 加速度プロット
        self.accel_plot.clear()
        self.accel_plot.addLegend()
        self.accel_plot.plot(self.time_array, self.accel_x,
                             pen=pen_x, name='X')
        self.accel_plot.plot(self.time_array, self.accel_y,
                             pen=pen_y, name='Y')
        self.accel_plot.plot(self.time_array, self.accel_z,
                             pen=pen_z, name='Z')
        self.accel_plot.setLabel('bottom', 'Time', 's')

        # 姿勢プロット
        pen_roll = pg.mkPen('#FF8787', width=2)   # ピンク系赤
        pen_pitch = pg.mkPen('#69DB7C', width=2)  # 明るい緑
        pen_yaw = pg.mkPen('#74C0FC', width=2)    # 明るい青

        self.attitude_plot.clear()
        self.attitude_plot.addLegend()
        self.attitude_plot.plot(self.time_array, self.roll,
                                pen=pen_roll, name='Roll')
        self.attitude_plot.plot(self.time_array, self.pitch,
                                pen=pen_pitch, name='Pitch')
        self.attitude_plot.plot(self.time_array, self.yaw,
                                pen=pen_yaw, name='Yaw')
        self.attitude_plot.setLabel('bottom', 'Time', 's')

        # ジャイロプロット
        self.gyro_plot.clear()
        self.gyro_plot.addLegend()
        self.gyro_plot.plot(self.time_array, self.gyro_x,
                            pen=pen_x, name='X')
        self.gyro_plot.plot(self.time_array, self.gyro_y,
                            pen=pen_y, name='Y')
        self.gyro_plot.plot(self.time_array, self.gyro_z,
                            pen=pen_z, name='Z')
        self.gyro_plot.setLabel('bottom', 'Time', 's')

        # 磁場プロット
        self.magnetic_plot.clear()
        self.magnetic_plot.addLegend()
        self.magnetic_plot.plot(self.time_array, self.mag_x,
                                pen=pen_x, name='X')
        self.magnetic_plot.plot(self.time_array, self.mag_y,
                                pen=pen_y, name='Y')
        self.magnetic_plot.plot(self.time_array, self.mag_z,
                                pen=pen_z, name='Z')
        self.magnetic_plot.setLabel('bottom', 'Time', 's')

        # GPSプロット
        self._plot_gps()

        # デッドレコニングプロット
        self._plot_dead_reckoning()

        # 統合航跡プロット
        self._plot_integrated_track()

    def _plot_gps(self):
        """GPSデータをプロット"""
        self.altitude_plot.clear()
        self.speed_plot.clear()
        self.accuracy_plot.clear()

        if len(self.gps_lat) == 0:
            self.gps_info_label.setText('No GPS data')
            return

        # 地図に軌跡を表示（精度情報付き）
        gps_data = [
            {'lat': float(lat), 'lon': float(lon), 'accuracy': float(acc)}
            for lat, lon, acc in zip(self.gps_lat, self.gps_lon, self.gps_accuracy)
        ]

        dr_coords = []
        if len(self.dr_lat) > 0:
            dr_coords = [[float(lat), float(lon)]
                         for lat, lon in zip(self.dr_lat, self.dr_lon)]

        ins_coords = []
        if len(self.ins_lat) > 0:
            ins_coords = [[float(lat), float(lon)]
                          for lat, lon in zip(self.ins_lat, self.ins_lon)]

        js = f'setGPSTrackWithAccuracy({json.dumps(gps_data)}, {json.dumps(dr_coords)}, {json.dumps(ins_coords)});'
        self.map_view.page().runJavaScript(js)

        # グラフ（鮮やかな色）
        self.altitude_plot.plot(self.gps_time, self.gps_alt,
                                pen=pg.mkPen('#5CD8FF', width=2))  # 明るいシアン
        self.altitude_plot.setLabel('bottom', 'Time', 's')

        self.speed_plot.plot(self.gps_time, self.gps_speed,
                             pen=pg.mkPen('#69DB7C', width=2))  # 明るい緑
        self.speed_plot.setLabel('bottom', 'Time', 's')

        self.accuracy_plot.plot(self.gps_time, self.gps_accuracy,
                                pen=pg.mkPen('#FFA94D', width=2))  # 明るいオレンジ
        self.accuracy_plot.setLabel('bottom', 'Time', 's')

        # GPS情報
        lat_center = np.mean(self.gps_lat)
        lon_center = np.mean(self.gps_lon)

        lat_to_m = 111320
        lon_to_m = 111320 * np.cos(np.radians(lat_center))

        x = (self.gps_lon - lon_center) * lon_to_m
        y = (self.gps_lat - lat_center) * lat_to_m

        total_dist = np.sum(np.sqrt(np.diff(x)**2 + np.diff(y)**2))
        avg_speed = np.mean(self.gps_speed)
        max_speed = np.max(self.gps_speed)
        avg_accuracy = np.mean(self.gps_accuracy)

        gps_text = f"""Points: {len(self.gps_lat)}
Center: {lat_center:.6f}, {lon_center:.6f}
Distance: {total_dist:.1f}m
Avg Speed: {avg_speed:.2f}m/s
Max Speed: {max_speed:.2f}m/s
Avg Accuracy: {avg_accuracy:.1f}m
Alt: {np.min(self.gps_alt):.1f} - {np.max(self.gps_alt):.1f}m"""

        self.gps_info_label.setText(gps_text)

    def _plot_dead_reckoning(self):
        """デッドレコニングデータをプロット"""
        self.dr_speed_plot.clear()
        self.dr_speed_plot.addLegend()
        self.dr_heading_plot.clear()
        self.dr_error_plot.clear()

        if len(self.gps_lat) == 0:
            self.dr_info_label.setText('No GPS data')
            return

        # DR地図（精度情報付き）
        gps_data = [
            {'lat': float(lat), 'lon': float(lon), 'accuracy': float(acc)}
            for lat, lon, acc in zip(self.gps_lat, self.gps_lon, self.gps_accuracy)
        ]

        dr_coords = []
        if len(self.dr_lat) > 0:
            dr_coords = [[float(lat), float(lon)]
                         for lat, lon in zip(self.dr_lat, self.dr_lon)]

        ins_coords = []
        if len(self.ins_lat) > 0:
            ins_coords = [[float(lat), float(lon)]
                          for lat, lon in zip(self.ins_lat, self.ins_lon)]

        js = f'setGPSTrackWithAccuracy({json.dumps(gps_data)}, {json.dumps(dr_coords)}, {json.dumps(ins_coords)});'
        self.dr_map_view.page().runJavaScript(js)

        # GPS速度も表示（明るい青）
        if len(self.gps_time) > 0:
            self.dr_speed_plot.plot(self.gps_time, self.gps_speed,
                                    pen=pg.mkPen('#4DABF7', width=2),
                                    name='GPS')

        if len(self.dr_lat) == 0:
            self.dr_info_label.setText('No Dead Reckoning data')
            return

        # DR速度（明るい紫）
        self.dr_speed_plot.plot(self.dr_time, self.dr_speed,
                                pen=pg.mkPen('#DA77F2', width=2),
                                name='DR')
        self.dr_speed_plot.setLabel('bottom', 'Time', 's')

        # DR方位（明るい紫）
        self.dr_heading_plot.plot(self.dr_time, self.dr_heading,
                                  pen=pg.mkPen('#DA77F2', width=2))
        self.dr_heading_plot.setLabel('bottom', 'Time', 's')

        # DR情報
        dr_text = f"""DR Points: {len(self.dr_lat)}
Duration: {self.dr_time[-1] - self.dr_time[0]:.1f}s
Avg Speed: {np.mean(self.dr_speed):.2f}m/s
Max Speed: {np.max(self.dr_speed):.2f}m/s"""

        self.dr_info_label.setText(dr_text)

    def _plot_integrated_track(self):
        """統合航跡を計算してプロット"""
        if len(self.records) == 0:
            return

        # 統合航跡を計算
        integrated_track = []  # [(lat, lon, track_type), ...]
        stats = {
            'total_distance': 0.0,
            'gps_count': 0,
            'fusion_count': 0,
            'memory_count': 0,
            'ins_count': 0,
            'total_accuracy': 0.0,
            'accuracy_count': 0
        }

        prev_lat, prev_lon = None, None

        for rec in self.records:
            gps = rec.get('gps', {})
            raw = gps.get('raw')
            fusion = rec.get('gps_ins_fusion')
            no_signal = gps.get('no_signal', True)

            lat, lon, track_type = None, None, None

            # 常にFusion位置を使用（連続性確保）、色はGPS精度/モードで決定
            if fusion:
                lat = fusion.get('latitude')
                lon = fusion.get('longitude')
                fusion_mode = fusion.get('mode', 'ins')

                # 色の決定: GPS精度が良ければGPS色、そうでなければFusion/Memory/INS色
                if raw and not no_signal:
                    accuracy = raw.get('horizontal_accuracy', 100)
                    stats['total_accuracy'] += accuracy
                    stats['accuracy_count'] += 1

                    if accuracy < 5:
                        track_type = 'gps_excellent'
                        stats['gps_count'] += 1
                    elif accuracy < 15:
                        track_type = 'gps_good'
                        stats['gps_count'] += 1
                    elif accuracy < 30:
                        track_type = 'gps_fair'
                        stats['gps_count'] += 1
                    else:
                        # GPS精度が悪い場合はFusionモードで色分け
                        if fusion_mode == 'memory_track':
                            track_type = 'memory'
                            stats['memory_count'] += 1
                        else:
                            track_type = 'fused'
                            stats['fusion_count'] += 1
                else:
                    # GPS無効時はFusionモードで色分け
                    if fusion_mode == 'memory_track':
                        track_type = 'memory'
                        stats['memory_count'] += 1
                    else:
                        track_type = 'ins'
                        stats['ins_count'] += 1

            elif raw and not no_signal:
                # Fusionがない場合はGPSを使用（後方互換性）
                lat = raw.get('latitude')
                lon = raw.get('longitude')
                accuracy = raw.get('horizontal_accuracy', 100)
                stats['total_accuracy'] += accuracy
                stats['accuracy_count'] += 1

                if accuracy < 5:
                    track_type = 'gps_excellent'
                elif accuracy < 15:
                    track_type = 'gps_good'
                elif accuracy < 30:
                    track_type = 'gps_fair'
                else:
                    track_type = 'gps_poor'
                stats['gps_count'] += 1

            if lat is not None and lon is not None:
                # 距離計算
                if prev_lat is not None:
                    dist = self._haversine_distance(prev_lat, prev_lon, lat, lon)
                    stats['total_distance'] += dist

                integrated_track.append((lat, lon, track_type))
                prev_lat, prev_lon = lat, lon

        if len(integrated_track) == 0:
            return

        # 統計計算
        total_points = stats['gps_count'] + stats['fusion_count'] + stats['memory_count'] + stats['ins_count']
        if total_points > 0:
            stats['gps_ratio'] = stats['gps_count'] / total_points * 100
            stats['fusion_ratio'] = stats['fusion_count'] / total_points * 100
            stats['memory_ratio'] = stats['memory_count'] / total_points * 100
        else:
            stats['gps_ratio'] = stats['fusion_ratio'] = stats['memory_ratio'] = 0

        if stats['accuracy_count'] > 0:
            stats['avg_accuracy'] = stats['total_accuracy'] / stats['accuracy_count']
        else:
            stats['avg_accuracy'] = 0

        # 航跡をタイプ別にセグメント化（連続性を保つ）
        segments = []
        current_segment = []
        current_type = None

        for lat, lon, track_type in integrated_track:
            if track_type != current_type:
                if len(current_segment) > 0:
                    segments.append((current_segment, current_type))
                    # 新しいセグメント開始時、前のセグメントの最後の点を含める（連続性確保）
                    current_segment = [current_segment[-1], [lat, lon]]
                else:
                    current_segment = [[lat, lon]]
                current_type = track_type
            else:
                current_segment.append([lat, lon])

        if len(current_segment) > 0:
            segments.append((current_segment, current_type))

        # 地図をクリア
        self.integrated_map_view.page().runJavaScript('clearTracks();')

        # セグメントを描画
        for coords, track_type in segments:
            if len(coords) >= 2:
                js = f'addTrackSegment({json.dumps(coords)}, "{track_type}");'
                self.integrated_map_view.page().runJavaScript(js)

        # 開始・終了マーカー
        if len(integrated_track) > 0:
            start_lat, start_lon, _ = integrated_track[0]
            end_lat, end_lon, _ = integrated_track[-1]
            js = f'setMarkers({start_lat}, {start_lon}, {end_lat}, {end_lon});'
            self.integrated_map_view.page().runJavaScript(js)

        # 全座標で地図をフィット
        all_coords = [[lat, lon] for lat, lon, _ in integrated_track]
        js = f'fitBounds({json.dumps(all_coords)});'
        self.integrated_map_view.page().runJavaScript(js)

        # 凡例と統計を表示
        stats_json = json.dumps(stats)
        self.integrated_map_view.page().runJavaScript(f'addLegend({stats_json});')
        self.integrated_map_view.page().runJavaScript(f'addStats({stats_json});')

        # 標高断面図を描画
        self._plot_elevation_profile(integrated_track)

    def _plot_elevation_profile(self, integrated_track):
        """標高断面図を描画"""
        self.elevation_plot.clear()

        if len(integrated_track) < 2:
            return

        # 座標リストを作成
        coords = [(lat, lon) for lat, lon, _ in integrated_track]

        # 距離→座標の対応表を保存（Region選択用）
        self._distance_to_coord = []

        # 高度融合による推定高度を計算
        altitude_fusion = AltitudeFusion()
        fused_distances = []
        fused_altitudes = []
        gps_distances = []
        gps_altitudes = []
        has_barometer_data = False  # 気圧計データの有無

        total_distance = 0.0
        prev_lat, prev_lon = None, None
        prev_time = None

        for i, rec in enumerate(self.records):
            gps = rec.get('gps', {})
            raw = gps.get('raw')
            sensors = rec.get('sensors', {})
            baro = sensors.get('barometer')
            accel = sensors.get('user_acceleration')
            timestamp = rec.get('timestamp', 0)

            # 位置を取得
            fusion_data = rec.get('gps_ins_fusion')
            lat, lon = None, None

            if fusion_data:
                lat = fusion_data.get('latitude')
                lon = fusion_data.get('longitude')
            elif raw and not gps.get('no_signal', True):
                lat = raw.get('latitude')
                lon = raw.get('longitude')

            if lat is None or lon is None:
                continue

            # 距離計算
            if prev_lat is not None:
                dist = self._haversine_distance(prev_lat, prev_lon, lat, lon)
                total_distance += dist

            # dt計算
            dt = 0.1
            if prev_time is not None:
                dt = timestamp - prev_time
            prev_time = timestamp

            # GPS高度
            gps_alt = None
            gps_v_acc = None
            if raw and not gps.get('no_signal', True):
                gps_alt = raw.get('altitude')
                gps_v_acc = raw.get('vertical_accuracy', -1)

                if gps_alt is not None and gps_alt != 0:
                    gps_distances.append(total_distance)
                    gps_altitudes.append(gps_alt)

            # 気圧計相対高度
            baro_relative = None
            if baro:
                baro_relative = baro.get('relative_altitude_m')
                if baro_relative is not None:
                    has_barometer_data = True

            # 加速度Z軸
            accel_z = None
            if accel:
                accel_z = accel.get('z')

            # 高度融合（気圧計データがある場合のみ記録）
            fused_alt = altitude_fusion.update(gps_alt, gps_v_acc, baro_relative, accel_z, dt)
            if fused_alt is not None and baro_relative is not None:
                fused_distances.append(total_distance)
                fused_altitudes.append(fused_alt)

            # 距離→座標の対応を記録
            self._distance_to_coord.append((total_distance, lat, lon))

            prev_lat, prev_lon = lat, lon

        # 国土地理院標高タイルから地形断面を取得
        try:
            gsi_api = GSIElevationAPI(zoom=14)
            # サンプリング間隔を調整（データ量に応じて）
            sample_interval = max(1, len(coords) // 100)
            terrain_profile = gsi_api.get_elevation_profile(coords, sample_interval=sample_interval)

            if terrain_profile:
                terrain_dist = [p[0] for p in terrain_profile]
                terrain_elev = [p[1] for p in terrain_profile]

                # 地形断面を塗りつぶしで描画
                terrain_brush = pg.mkBrush('#4a5568')
                fill = pg.FillBetweenItem(
                    pg.PlotDataItem(terrain_dist, terrain_elev),
                    pg.PlotDataItem(terrain_dist, [min(terrain_elev) - 10] * len(terrain_dist)),
                    brush=terrain_brush
                )
                self.elevation_plot.addItem(fill)

                # 地形の線も描画
                self.elevation_plot.plot(terrain_dist, terrain_elev,
                                         pen=pg.mkPen('#718096', width=2),
                                         name='地形標高')
        except Exception as e:
            print(f'標高タイル取得エラー: {e}')

        # GPS高度をプロット（地図のGPS Good色と統一: #118ab2）
        if gps_distances and gps_altitudes:
            self.elevation_plot.plot(gps_distances, gps_altitudes,
                                     pen=pg.mkPen('#118ab2', width=2),
                                     name='GPS高度')

        # 融合高度をプロット（気圧計データがある場合のみ、地図のFusion色と統一: #00CED1）
        if fused_distances and fused_altitudes and has_barometer_data:
            self.elevation_plot.plot(fused_distances, fused_altitudes,
                                     pen=pg.mkPen('#00CED1', width=2),
                                     name='融合高度(GPS+気圧計)')

        # LinearRegionItem（区間選択）を追加
        if self._distance_to_coord:
            max_dist = self._distance_to_coord[-1][0] if self._distance_to_coord else 1000
            # 初期範囲は全体の20-40%
            initial_region = [max_dist * 0.2, max_dist * 0.4]

            self._elevation_region = pg.LinearRegionItem(
                values=initial_region,
                brush=pg.mkBrush(255, 165, 0, 50),  # オレンジ半透明
                movable=True
            )
            self.elevation_plot.addItem(self._elevation_region)

            # 範囲変更時のコールバック
            self._elevation_region.sigRegionChanged.connect(self._on_elevation_region_changed)

            # 初期表示
            self._on_elevation_region_changed()

    def _on_elevation_region_changed(self):
        """断面図の区間選択が変更された時のコールバック"""
        if not hasattr(self, '_elevation_region') or not hasattr(self, '_distance_to_coord'):
            return

        region = self._elevation_region.getRegion()
        start_dist, end_dist = region

        # 距離から座標を検索
        start_coord = None
        end_coord = None

        for dist, lat, lon in self._distance_to_coord:
            if start_coord is None and dist >= start_dist:
                start_coord = (lat, lon)
            if dist <= end_dist:
                end_coord = (lat, lon)

        if start_coord and end_coord:
            js = f'setRegionMarkers({start_coord[0]}, {start_coord[1]}, {end_coord[0]}, {end_coord[1]});'
            self.integrated_map_view.page().runJavaScript(js)

    def _haversine_distance(self, lat1, lon1, lat2, lon2):
        """2点間の距離をHaversine公式で計算（メートル）"""
        R = 6378137.0  # 地球半径

        lat1_rad = np.radians(lat1)
        lat2_rad = np.radians(lat2)
        dlat = np.radians(lat2 - lat1)
        dlon = np.radians(lon2 - lon1)

        a = np.sin(dlat/2)**2 + np.cos(lat1_rad) * np.cos(lat2_rad) * np.sin(dlon/2)**2
        c = 2 * np.arctan2(np.sqrt(a), np.sqrt(1-a))

        return R * c


def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    # ダークテーマ
    pg.setConfigOption('background', '#2d2d44')
    pg.setConfigOption('foreground', '#edf2f4')

    # コマンドライン引数でフォルダを指定（省略時はカレントフォルダ）
    folder_path = sys.argv[1] if len(sys.argv) > 1 else None

    viewer = SensorLogViewer(folder_path)
    viewer.show()

    sys.exit(app.exec())


if __name__ == '__main__':
    main()
