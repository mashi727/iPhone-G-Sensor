# -*- coding: utf-8 -*-
"""
Sensor Logger Viewer - ログデータ可視化アプリ

PySide6 + PyQtGraph + 国土地理院地図 によるセンサーログの可視化ツール
"""

import sys
import json
import math
import tempfile
import webbrowser
import numpy as np
import urllib.request
from pathlib import Path
from functools import lru_cache

# 高速ストリーミングJSON読み込み
try:
    import ijson
    IJSON_AVAILABLE = True
except ImportError:
    IJSON_AVAILABLE = False

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTabWidget, QFileDialog, QPushButton, QLabel,
    QGroupBox, QStatusBar, QComboBox, QTreeView, QHeaderView, QFileSystemModel,
    QProgressDialog
)
from PySide6.QtCore import Qt, QUrl, QDir, QThread, Signal
from PySide6.QtGui import QAction, QShortcut, QKeySequence
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineProfile
from PySide6.QtWebChannel import QWebChannel

import pyqtgraph as pg

# PyQtGraph設定（OpenGL無効、antialias無効で高速化）
pg.setConfigOptions(antialias=False, useOpenGL=False)

# 間引き表示の設定
MAX_DISPLAY_POINTS = 2000  # 表示する最大ポイント数
MAP_MAX_POINTS = 1000      # 地図上の最大ポイント数
MAX_LOAD_RECORDS = 10000   # 読み込み時の最大レコード数（大きなファイル対策）
STREAMING_THRESHOLD_MB = 20  # このサイズ以上でストリーミング読み込みを使用

# 地図タイルの取得元にアプリを識別させる（OpenStreetMap のタイル利用ポリシーが求める）
APP_USER_AGENT_SUFFIX = 'SensorLogViewer (+https://github.com/mashi727/iPhone-G-Sensor)'


def decimate_data(data, max_points=MAX_DISPLAY_POINTS):
    """データを間引いて指定ポイント数以下にする（LTTB風の簡易版）

    Args:
        data: numpy配列またはリスト
        max_points: 最大ポイント数

    Returns:
        間引かれたデータ、インデックス配列
    """
    if len(data) <= max_points:
        return np.array(data), np.arange(len(data))

    # 間引き間隔を計算
    step = len(data) / max_points
    indices = np.round(np.arange(0, len(data), step)).astype(int)
    indices = np.clip(indices, 0, len(data) - 1)
    indices = np.unique(indices)  # 重複を除去

    # 最後のポイントを含める
    if indices[-1] != len(data) - 1:
        indices = np.append(indices, len(data) - 1)

    return np.array(data)[indices], indices


def decimate_xy(x, y, max_points=MAX_DISPLAY_POINTS):
    """X,Yペアのデータを間引く

    Args:
        x, y: numpy配列
        max_points: 最大ポイント数

    Returns:
        間引かれた(x, y)
    """
    if len(x) <= max_points:
        return x, y

    _, indices = decimate_data(x, max_points)
    return np.array(x)[indices], np.array(y)[indices]


def decimate_coords(coords, max_points=MAP_MAX_POINTS):
    """座標リストを間引く（地図用）

    Args:
        coords: [[lat, lon], ...] または [{'lat':..., 'lon':...}, ...]形式
        max_points: 最大ポイント数

    Returns:
        間引かれた座標リスト
    """
    if len(coords) <= max_points:
        return coords

    step = len(coords) / max_points
    indices = np.round(np.arange(0, len(coords), step)).astype(int)
    indices = np.clip(indices, 0, len(coords) - 1)
    indices = np.unique(indices)

    # 最初と最後を含める
    if indices[0] != 0:
        indices = np.insert(indices, 0, 0)
    if indices[-1] != len(coords) - 1:
        indices = np.append(indices, len(coords) - 1)

    return [coords[i] for i in indices]


def setup_plot_downsampling(plot_widget):
    """PlotWidgetにダウンサンプリングとクリッピングを設定する

    Args:
        plot_widget: pg.PlotWidget インスタンス
    """
    # ダウンサンプリングを無効化（問題の原因の可能性）
    # plot_widget.setDownsampling(auto=True, mode='peak')
    # plot_widget.setClipToView(True)
    pass


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
            osm: L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
                attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
                maxZoom: 19
            })
        };

        var currentTileLayer = tileLayers.osm;
        currentTileLayer.addTo(map);

        function setMapType(mapType) {
            map.removeLayer(currentTileLayer);
            currentTileLayer = tileLayers[mapType] || tileLayers.osm;
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

        // 全データを保持（ズーム時の再描画用）
        var fullGpsData = [];
        var fullDrCoords = [];
        var fullInsCoords = [];

        // ズームレベルに応じた間引き関数
        function decimateArray(arr, maxPoints) {
            if (arr.length <= maxPoints) return arr;
            var step = arr.length / maxPoints;
            var result = [];
            for (var i = 0; i < maxPoints; i++) {
                var idx = Math.min(Math.floor(i * step), arr.length - 1);
                result.push(arr[idx]);
            }
            // 最後の点を必ず含める
            if (result[result.length - 1] !== arr[arr.length - 1]) {
                result.push(arr[arr.length - 1]);
            }
            return result;
        }

        // ズームレベルに応じた最大ポイント数を計算
        function getMaxPointsForZoom(zoom) {
            // ズーム 5以下: 500点、ズーム 10: 1000点、ズーム 15: 2000点、ズーム 18以上: 全点
            if (zoom >= 18) return Infinity;
            if (zoom >= 15) return 3000;
            if (zoom >= 12) return 2000;
            if (zoom >= 10) return 1500;
            if (zoom >= 8) return 1000;
            return 500;
        }

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
            // 全データを保持
            fullGpsData = gpsData;
            fullDrCoords = drCoords || [];
            fullInsCoords = insCoords || [];

            // 現在のズームレベルで描画
            redrawTracksForZoom();

            // 全体が見えるようにフィット
            var allCoords = [];
            gpsData.forEach(function(p) { allCoords.push([p.lat, p.lon]); });
            fullDrCoords.forEach(function(c) { allCoords.push(c); });
            fullInsCoords.forEach(function(c) { allCoords.push(c); });

            if (allCoords.length > 0) {
                map.fitBounds(L.latLngBounds(allCoords), {padding: [30, 30]});
            }
        }

        // ズームレベルに応じて軌跡を再描画
        function redrawTracksForZoom() {
            clearMap();

            if (fullGpsData.length === 0) return;

            var zoom = map.getZoom();
            var maxPoints = getMaxPointsForZoom(zoom);

            // 間引き適用
            var gpsData = decimateArray(fullGpsData, maxPoints);
            var drCoords = decimateArray(fullDrCoords, maxPoints);
            var insCoords = decimateArray(fullInsCoords, maxPoints);

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

            // 開始点（白枠付き緑）- 全データの最初と最後を使用
            if (fullGpsData.length > 0) {
                var startPoint = fullGpsData[0];
                var endPoint = fullGpsData[fullGpsData.length - 1];

                startMarker = L.circleMarker([startPoint.lat, startPoint.lon], {
                    radius: 10,
                    fillColor: '#06d6a0',
                    color: '#fff',
                    weight: 3,
                    fillOpacity: 1
                }).addTo(map).bindPopup('Start');

                // 終了点（白枠付き赤）
                endMarker = L.circleMarker([endPoint.lat, endPoint.lon], {
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

                // INS終了点マーカー（全データの最後）
                if (fullInsCoords.length > 0) {
                    var insEnd = fullInsCoords[fullInsCoords.length - 1];
                    insEndMarker = L.circleMarker(insEnd, {
                        radius: 8,
                        fillColor: insColor,
                        color: '#fff',
                        weight: 2,
                        fillOpacity: 1
                    }).addTo(map).bindPopup('INS End');
                }
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
        }

        // ズームイベントでトラックを再描画
        map.on('zoomend', function() {
            if (fullGpsData.length > 0) {
                redrawTracksForZoom();
            }
        });

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
            osm: L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
                attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
                maxZoom: 19
            })
        };

        var currentTileLayer = tileLayers.osm;
        currentTileLayer.addTo(map);

        function setMapType(mapType) {
            map.removeLayer(currentTileLayer);
            currentTileLayer = tileLayers[mapType] || tileLayers.osm;
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

        // 全セグメントデータを保持（ズーム時の再描画用）
        var fullSegments = [];
        var fullStartEnd = null;
        var fullStats = null;

        // ズームレベルに応じた間引き関数
        function decimateArray(arr, maxPoints) {
            if (arr.length <= maxPoints) return arr;
            var step = arr.length / maxPoints;
            var result = [];
            for (var i = 0; i < maxPoints; i++) {
                var idx = Math.min(Math.floor(i * step), arr.length - 1);
                result.push(arr[idx]);
            }
            if (result[result.length - 1] !== arr[arr.length - 1]) {
                result.push(arr[arr.length - 1]);
            }
            return result;
        }

        // ズームレベルに応じた最大ポイント数を計算
        function getMaxPointsForZoom(zoom) {
            if (zoom >= 18) return Infinity;
            if (zoom >= 15) return 3000;
            if (zoom >= 12) return 2000;
            if (zoom >= 10) return 1500;
            if (zoom >= 8) return 1000;
            return 500;
        }

        function clearTracks() {
            trackLayers.forEach(function(layer) {
                map.removeLayer(layer);
            });
            trackLayers = [];
            fullSegments = [];  // 全セグメントデータもクリア
            if (startMarker) { map.removeLayer(startMarker); startMarker = null; }
            if (endMarker) { map.removeLayer(endMarker); endMarker = null; }
            if (legendControl) { map.removeControl(legendControl); legendControl = null; }
            if (statsControl) { map.removeControl(statsControl); statsControl = null; }
        }

        function addTrackSegment(coords, trackType) {
            // セグメントを保存
            fullSegments.push({ coords: coords, trackType: trackType });
        }

        function drawSegment(coords, trackType) {
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

        // ズームレベルに応じてセグメントを再描画
        function redrawSegmentsForZoom() {
            // 既存のレイヤーをクリア（マーカー類は残す）
            trackLayers.forEach(function(layer) {
                map.removeLayer(layer);
            });
            trackLayers = [];

            var zoom = map.getZoom();
            var maxPoints = getMaxPointsForZoom(zoom);
            var pointsPerSegment = Math.max(50, Math.floor(maxPoints / Math.max(1, fullSegments.length)));

            // セグメントを間引いて描画
            fullSegments.forEach(function(seg) {
                var decimatedCoords = decimateArray(seg.coords, pointsPerSegment);
                drawSegment(decimatedCoords, seg.trackType);
            });
        }

        // ズームイベントでセグメントを再描画
        map.on('zoomend', function() {
            if (fullSegments.length > 0) {
                redrawSegmentsForZoom();
            }
        });

        // 初回描画（addTrackSegmentが全て呼ばれた後に呼び出す）
        function finishAddingSegments() {
            redrawSegmentsForZoom();
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

# Three.js 3D表示用HTML
THREEJS_3D_HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>3D Flight Track</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
    <style>
        html, body {
            width: 100%; height: 100%;
            margin: 0; padding: 0;
            overflow: hidden;
            background: #1a1a2e;
        }
        #container {
            width: 100%; height: 100%;
        }
        .control-panel {
            position: absolute;
            top: 10px;
            left: 10px;
            z-index: 1000;
            background: rgba(38, 38, 38, 0.95);
            padding: 12px;
            border-radius: 8px;
            color: #fff;
            font-family: -apple-system, sans-serif;
            font-size: 12px;
        }
        .control-panel button {
            background: #4CAF50;
            color: white;
            border: none;
            padding: 8px 16px;
            margin: 4px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 12px;
        }
        .control-panel button:hover {
            background: #45a049;
        }
        .control-panel button.stop {
            background: #f44336;
        }
        .control-panel button.active {
            background: #2196F3;
        }
        .speed-control {
            margin-top: 8px;
        }
        .speed-control label {
            display: block;
            margin-bottom: 4px;
        }
        .speed-control input {
            width: 100%;
        }
        .info-panel {
            position: absolute;
            bottom: 10px;
            left: 10px;
            z-index: 1000;
            background: rgba(38, 38, 38, 0.95);
            padding: 12px;
            border-radius: 8px;
            color: #fff;
            font-family: monospace;
            font-size: 11px;
            min-width: 220px;
        }
        .legend {
            position: absolute;
            top: 10px;
            right: 10px;
            z-index: 1000;
            background: rgba(38, 38, 38, 0.95);
            padding: 12px;
            border-radius: 8px;
            color: #fff;
            font-family: -apple-system, sans-serif;
            font-size: 11px;
        }
        .legend-item {
            display: flex;
            align-items: center;
            margin: 4px 0;
        }
        .legend-color {
            width: 20px;
            height: 4px;
            margin-right: 8px;
            border-radius: 2px;
        }
        .help-text {
            position: absolute;
            bottom: 10px;
            right: 10px;
            z-index: 1000;
            background: rgba(38, 38, 38, 0.95);
            padding: 10px;
            border-radius: 8px;
            color: #888;
            font-family: -apple-system, sans-serif;
            font-size: 10px;
        }
    </style>
</head>
<body>
    <div id="container"></div>
    <div class="control-panel">
        <div>
            <button id="btnPlay" onclick="playAnimation()">▶ Play</button>
            <button id="btnPause" onclick="pauseAnimation()" class="stop">⏸ Pause</button>
            <button onclick="resetAnimation()">↺ Reset</button>
        </div>
        <div class="speed-control">
            <label>Speed: <span id="speedValue">10x</span></label>
            <input type="range" id="speedSlider" min="1" max="100" value="10" oninput="changeSpeed(this.value)">
        </div>
        <div style="margin-top: 8px;">
            <button onclick="resetCamera()">📷 Reset View</button>
            <button onclick="topView()">⬇ Top View</button>
        </div>
        <div style="margin-top: 8px;">
            <button onclick="sideView()">➡ Side View</button>
            <button onclick="followMode()">🛩 Follow</button>
        </div>
    </div>
    <div class="info-panel" id="infoPanel">
        <div>Time: <span id="currentTime">--:--:--</span></div>
        <div>Altitude: <span id="currentAlt">--- m</span></div>
        <div>Speed: <span id="currentSpeed">--- m/s</span></div>
        <div>Position: <span id="currentPos">---, ---</span></div>
        <div>Progress: <span id="progress">0%</span></div>
    </div>
    <div class="legend">
        <div style="font-weight: bold; margin-bottom: 6px;">Altitude Color</div>
        <div class="legend-item"><div class="legend-color" style="background: #00ff00;"></div>Low</div>
        <div class="legend-item"><div class="legend-color" style="background: #ffff00;"></div>Medium</div>
        <div class="legend-item"><div class="legend-color" style="background: #ff8800;"></div>High</div>
        <div class="legend-item"><div class="legend-color" style="background: #ff0000;"></div>Very High</div>
    </div>
    <div class="help-text">
        Drag: Rotate | Scroll: Zoom | Right-drag: Pan
    </div>
    <script>
        var scene, camera, renderer, controls;
        var trackLine, currentMarker, groundPlane;
        var trackData = [];
        var minAlt = 0, maxAlt = 1000;
        var centerLat = 0, centerLon = 0;
        var scaleX = 1, scaleY = 1, scaleZ = 0.5;
        var isPlaying = false;
        var playSpeed = 10;
        var currentIndex = 0;
        var lastTime = 0;
        var isFollowing = false;

        // 初期化
        function init() {
            var container = document.getElementById('container');

            // シーン
            scene = new THREE.Scene();
            scene.background = new THREE.Color(0x1a1a2e);

            // カメラ
            camera = new THREE.PerspectiveCamera(60, window.innerWidth / window.innerHeight, 0.1, 10000);
            camera.position.set(500, 500, 500);
            camera.lookAt(0, 0, 0);

            // レンダラー
            renderer = new THREE.WebGLRenderer({ antialias: true });
            renderer.setSize(window.innerWidth, window.innerHeight);
            renderer.setPixelRatio(window.devicePixelRatio);
            container.appendChild(renderer.domElement);

            // ライト
            var ambientLight = new THREE.AmbientLight(0xffffff, 0.6);
            scene.add(ambientLight);

            var directionalLight = new THREE.DirectionalLight(0xffffff, 0.8);
            directionalLight.position.set(100, 200, 100);
            scene.add(directionalLight);

            // グリッド
            var gridHelper = new THREE.GridHelper(2000, 40, 0x444444, 0x333333);
            scene.add(gridHelper);

            // 軸ヘルパー
            var axesHelper = new THREE.AxesHelper(100);
            scene.add(axesHelper);

            // マウスコントロール（簡易実装）
            setupControls();

            // リサイズ対応
            window.addEventListener('resize', onWindowResize, false);

            animate();
        }

        // マウスコントロール
        var isDragging = false;
        var isRightDragging = false;
        var previousMousePosition = { x: 0, y: 0 };
        var spherical = { theta: Math.PI / 4, phi: Math.PI / 4, radius: 800 };

        function setupControls() {
            var container = document.getElementById('container');

            container.addEventListener('mousedown', function(e) {
                if (e.button === 0) isDragging = true;
                if (e.button === 2) isRightDragging = true;
                previousMousePosition = { x: e.clientX, y: e.clientY };
            });

            container.addEventListener('mouseup', function(e) {
                isDragging = false;
                isRightDragging = false;
            });

            container.addEventListener('mouseleave', function() {
                isDragging = false;
                isRightDragging = false;
            });

            container.addEventListener('mousemove', function(e) {
                var deltaX = e.clientX - previousMousePosition.x;
                var deltaY = e.clientY - previousMousePosition.y;

                if (isDragging) {
                    spherical.theta -= deltaX * 0.005;
                    spherical.phi = Math.max(0.1, Math.min(Math.PI - 0.1, spherical.phi - deltaY * 0.005));
                    updateCameraPosition();
                }

                if (isRightDragging) {
                    var offset = new THREE.Vector3();
                    offset.x = -deltaX * spherical.radius * 0.001;
                    offset.z = -deltaY * spherical.radius * 0.001;
                    camera.position.add(offset);
                }

                previousMousePosition = { x: e.clientX, y: e.clientY };
            });

            container.addEventListener('wheel', function(e) {
                e.preventDefault();
                spherical.radius *= (1 + e.deltaY * 0.001);
                spherical.radius = Math.max(50, Math.min(5000, spherical.radius));
                updateCameraPosition();
            });

            container.addEventListener('contextmenu', function(e) {
                e.preventDefault();
            });
        }

        function updateCameraPosition() {
            if (isFollowing && currentMarker) {
                var target = currentMarker.position.clone();
                camera.position.x = target.x + spherical.radius * Math.sin(spherical.phi) * Math.cos(spherical.theta);
                camera.position.y = target.y + spherical.radius * Math.cos(spherical.phi);
                camera.position.z = target.z + spherical.radius * Math.sin(spherical.phi) * Math.sin(spherical.theta);
                camera.lookAt(target);
            } else {
                camera.position.x = spherical.radius * Math.sin(spherical.phi) * Math.cos(spherical.theta);
                camera.position.y = spherical.radius * Math.cos(spherical.phi);
                camera.position.z = spherical.radius * Math.sin(spherical.phi) * Math.sin(spherical.theta);
                camera.lookAt(0, 0, 0);
            }
        }

        function onWindowResize() {
            camera.aspect = window.innerWidth / window.innerHeight;
            camera.updateProjectionMatrix();
            renderer.setSize(window.innerWidth, window.innerHeight);
        }

        // 高度に基づく色
        function getColorByAltitude(alt) {
            var ratio = (alt - minAlt) / Math.max(maxAlt - minAlt, 1);
            ratio = Math.max(0, Math.min(1, ratio));

            var r, g, b;
            if (ratio < 0.33) {
                r = 0; g = 1; b = 0;
                var t = ratio / 0.33;
                r = t; g = 1;
            } else if (ratio < 0.66) {
                var t = (ratio - 0.33) / 0.33;
                r = 1; g = 1 - t * 0.5; b = 0;
            } else {
                var t = (ratio - 0.66) / 0.34;
                r = 1; g = 0.5 - t * 0.5; b = 0;
            }
            return new THREE.Color(r, g, b);
        }

        // 座標変換（緯度経度→ローカル座標）
        function latLonToLocal(lat, lon, alt) {
            var x = (lon - centerLon) * 111320 * Math.cos(centerLat * Math.PI / 180) * scaleX;
            var z = (lat - centerLat) * 110540 * scaleY;
            var y = (alt - minAlt) * scaleZ;
            return new THREE.Vector3(x, y, z);
        }

        // 航跡データを読み込み
        function loadTrackData(data) {
            trackData = data;
            if (trackData.length < 2) return;

            // 既存のオブジェクトを削除
            if (trackLine) scene.remove(trackLine);
            if (currentMarker) scene.remove(currentMarker);

            // 範囲を計算
            var lats = trackData.map(function(p) { return p.lat; });
            var lons = trackData.map(function(p) { return p.lon; });
            var alts = trackData.map(function(p) { return p.alt || 0; });

            centerLat = (Math.min.apply(null, lats) + Math.max.apply(null, lats)) / 2;
            centerLon = (Math.min.apply(null, lons) + Math.max.apply(null, lons)) / 2;
            minAlt = Math.min.apply(null, alts.filter(function(a) { return a > 0; })) || 0;
            maxAlt = Math.max.apply(null, alts) || 1000;

            // スケール調整
            var latRange = Math.max.apply(null, lats) - Math.min.apply(null, lats);
            var lonRange = Math.max.apply(null, lons) - Math.min.apply(null, lons);
            var maxRange = Math.max(latRange * 110540, lonRange * 111320 * Math.cos(centerLat * Math.PI / 180));
            var targetSize = 800;
            scaleX = targetSize / Math.max(maxRange, 1);
            scaleY = scaleX;
            scaleZ = targetSize / Math.max(maxAlt - minAlt, 100) * 0.3;

            // 航跡ライン（頂点カラー付き）
            var geometry = new THREE.BufferGeometry();
            var positions = [];
            var colors = [];

            for (var i = 0; i < trackData.length; i++) {
                var p = trackData[i];
                var pos = latLonToLocal(p.lat, p.lon, p.alt || 0);
                positions.push(pos.x, pos.y, pos.z);

                var color = getColorByAltitude(p.alt || 0);
                colors.push(color.r, color.g, color.b);
            }

            geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
            geometry.setAttribute('color', new THREE.Float32BufferAttribute(colors, 3));

            var material = new THREE.LineBasicMaterial({
                vertexColors: true,
                linewidth: 2
            });

            trackLine = new THREE.Line(geometry, material);
            scene.add(trackLine);

            // 地面への投影線（影）
            var groundPositions = [];
            for (var i = 0; i < trackData.length; i++) {
                var p = trackData[i];
                var pos = latLonToLocal(p.lat, p.lon, 0);
                groundPositions.push(pos.x, 0, pos.z);
            }
            var groundGeometry = new THREE.BufferGeometry();
            groundGeometry.setAttribute('position', new THREE.Float32BufferAttribute(groundPositions, 3));
            var groundMaterial = new THREE.LineBasicMaterial({ color: 0x444444, linewidth: 1 });
            var groundLine = new THREE.Line(groundGeometry, groundMaterial);
            scene.add(groundLine);

            // 現在位置マーカー
            var markerGeometry = new THREE.SphereGeometry(8, 16, 16);
            var markerMaterial = new THREE.MeshPhongMaterial({ color: 0xff0000, emissive: 0x440000 });
            currentMarker = new THREE.Mesh(markerGeometry, markerMaterial);
            var startPos = latLonToLocal(trackData[0].lat, trackData[0].lon, trackData[0].alt || 0);
            currentMarker.position.copy(startPos);
            scene.add(currentMarker);

            // 開始・終了マーカー
            var startMarkerGeo = new THREE.ConeGeometry(10, 20, 8);
            var startMarkerMat = new THREE.MeshPhongMaterial({ color: 0x00ff00, emissive: 0x004400 });
            var startMarker = new THREE.Mesh(startMarkerGeo, startMarkerMat);
            startMarker.position.copy(startPos);
            startMarker.position.y += 15;
            startMarker.rotation.x = Math.PI;
            scene.add(startMarker);

            var endPos = latLonToLocal(trackData[trackData.length-1].lat, trackData[trackData.length-1].lon, trackData[trackData.length-1].alt || 0);
            var endMarkerGeo = new THREE.ConeGeometry(10, 20, 8);
            var endMarkerMat = new THREE.MeshPhongMaterial({ color: 0xff4444, emissive: 0x440000 });
            var endMarker = new THREE.Mesh(endMarkerGeo, endMarkerMat);
            endMarker.position.copy(endPos);
            endMarker.position.y += 15;
            endMarker.rotation.x = Math.PI;
            scene.add(endMarker);

            // カメラ位置をリセット
            spherical.radius = targetSize * 1.5;
            updateCameraPosition();

            currentIndex = 0;
            updateInfoPanel();
        }

        function updateInfoPanel() {
            if (trackData.length === 0) return;

            var p = trackData[currentIndex];
            document.getElementById('currentAlt').textContent = (p.alt || 0).toFixed(1) + ' m';
            document.getElementById('currentPos').textContent = p.lat.toFixed(5) + ', ' + p.lon.toFixed(5);

            if (p.time) {
                var date = new Date(p.time);
                document.getElementById('currentTime').textContent = date.toLocaleTimeString();
            }

            // 速度計算
            if (currentIndex > 0) {
                var prev = trackData[currentIndex - 1];
                var dt = (new Date(p.time) - new Date(prev.time)) / 1000;
                if (dt > 0) {
                    var dx = (p.lon - prev.lon) * 111320 * Math.cos(p.lat * Math.PI / 180);
                    var dy = (p.lat - prev.lat) * 110540;
                    var dz = (p.alt || 0) - (prev.alt || 0);
                    var dist = Math.sqrt(dx*dx + dy*dy + dz*dz);
                    document.getElementById('currentSpeed').textContent = (dist / dt).toFixed(1) + ' m/s';
                }
            }

            document.getElementById('progress').textContent = Math.round(currentIndex / (trackData.length - 1) * 100) + '%';
        }

        function animate() {
            requestAnimationFrame(animate);

            var now = Date.now();
            if (isPlaying && trackData.length > 0) {
                var elapsed = (now - lastTime) / 1000;
                if (elapsed > 0.05 / playSpeed) {
                    currentIndex = Math.min(currentIndex + 1, trackData.length - 1);
                    if (currentIndex >= trackData.length - 1) {
                        currentIndex = 0;
                    }
                    var p = trackData[currentIndex];
                    var pos = latLonToLocal(p.lat, p.lon, p.alt || 0);
                    currentMarker.position.copy(pos);
                    updateInfoPanel();
                    lastTime = now;

                    if (isFollowing) {
                        updateCameraPosition();
                    }
                }
            }

            renderer.render(scene, camera);
        }

        function playAnimation() {
            isPlaying = true;
            lastTime = Date.now();
            document.getElementById('btnPlay').classList.add('active');
            document.getElementById('btnPause').classList.remove('active');
        }

        function pauseAnimation() {
            isPlaying = false;
            document.getElementById('btnPause').classList.add('active');
            document.getElementById('btnPlay').classList.remove('active');
        }

        function resetAnimation() {
            currentIndex = 0;
            if (trackData.length > 0) {
                var p = trackData[0];
                var pos = latLonToLocal(p.lat, p.lon, p.alt || 0);
                currentMarker.position.copy(pos);
            }
            updateInfoPanel();
        }

        function changeSpeed(value) {
            playSpeed = parseInt(value);
            document.getElementById('speedValue').textContent = value + 'x';
        }

        function resetCamera() {
            isFollowing = false;
            spherical.theta = Math.PI / 4;
            spherical.phi = Math.PI / 4;
            updateCameraPosition();
        }

        function topView() {
            isFollowing = false;
            spherical.theta = 0;
            spherical.phi = 0.1;
            updateCameraPosition();
        }

        function sideView() {
            isFollowing = false;
            spherical.theta = 0;
            spherical.phi = Math.PI / 2;
            updateCameraPosition();
        }

        function followMode() {
            isFollowing = !isFollowing;
            if (isFollowing) {
                updateCameraPosition();
            }
        }

        // 初期化
        init();
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

    # 標高タイルの提供範囲（日本の領域を包含する矩形）。範囲外はタイルが存在しないため問い合わせない
    COVERAGE_LAT = (20.0, 46.0)
    COVERAGE_LON = (122.0, 154.0)

    def __init__(self, zoom=15):
        self.zoom = zoom
        self._cache = {}

    @classmethod
    def in_coverage(cls, lat, lon):
        """座標が標高タイルの提供範囲内か"""
        return (cls.COVERAGE_LAT[0] <= lat <= cls.COVERAGE_LAT[1] and
                cls.COVERAGE_LON[0] <= lon <= cls.COVERAGE_LON[1])

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
                req = urllib.request.Request(url, headers={'User-Agent': APP_USER_AGENT_SUFFIX})
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
        if not self.in_coverage(lat, lon):
            return None

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

    def get_elevation_profile(self, coords, sample_interval=10, should_stop=None):
        """
        経路に沿った標高プロファイルを取得

        coords: [(lat, lon), ...] 座標リスト
        sample_interval: サンプリング間隔（インデックス）
        should_stop: 中断判定の関数（Trueを返すと打ち切る）

        returns: [(distance, elevation), ...] 距離と標高のリスト
        """
        profile = []
        total_distance = 0.0
        prev_lat, prev_lon = None, None

        for i, (lat, lon) in enumerate(coords):
            if should_stop and should_stop():
                return []
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

    @staticmethod
    def _haversine(lat1, lon1, lat2, lon2):
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


class LogSummary:
    """
    全レコードを1パスで走査する要約。

    表示用のレコードは間引かれるため、GPS途絶・DR作動・記録欠落のような
    稀な事象は取りこぼされる。これらは間引き前の全レコードで集計する。
    """

    # 直前レコードからこれ以上空いたら記録欠落とみなす（ロガーの記録間隔は100ms）
    GAP_THRESHOLD_S = 3.0

    def __init__(self):
        self.total = 0
        self.gps_valid = 0
        self.gaps = []          # [(開始時刻, 欠落秒数), ...]
        self.dr_points = []     # [(t, lat, lon, speed, heading, drift_m, episode_index), ...]
        self.dr_episodes = []   # [{'start', 'end', 'n', 'after_gap'}, ...]
        self._t0 = None
        self._prev_t = None
        self._prev_dr_active = False

    def add(self, rec):
        t_abs = rec.get('timestamp')
        if t_abs is None:
            return
        t_abs = float(t_abs)
        if self._t0 is None:
            self._t0 = t_abs
        t = t_abs - self._t0
        self.total += 1

        gap_before = 0.0
        if self._prev_t is not None:
            gap_before = t - self._prev_t
            if gap_before > self.GAP_THRESHOLD_S:
                self.gaps.append((self._prev_t, gap_before))
        self._prev_t = t

        gps = rec.get('gps') or {}
        # stale: ロガー v1.2.0 以降、復帰直後に停止前から残っている測位に付く
        if gps.get('raw') and not gps.get('no_signal', True) and not gps.get('stale'):
            self.gps_valid += 1

        dr = rec.get('dead_reckoning') or {}
        result = dr.get('result') if dr.get('active') else None
        if not result:
            self._prev_dr_active = False
            return

        if not self._prev_dr_active:
            # 記録再開直後の作動は、ロガーが保持していたGPS時刻が古いことによる
            # タイムアウト判定であり、実際のGPS途絶とは区別する
            self.dr_episodes.append({'start': t, 'end': t, 'n': 0,
                                     'after_gap': gap_before > self.GAP_THRESHOLD_S})
        episode = self.dr_episodes[-1]
        episode['end'] = t
        episode['n'] += 1
        self._prev_dr_active = True

        lat = float(result.get('latitude', 0))
        lon = float(result.get('longitude', 0))
        drift = float('nan')
        last_lat, last_lon = result.get('last_gps_lat'), result.get('last_gps_lon')
        if last_lat is not None and last_lon is not None:
            drift = GSIElevationAPI._haversine(float(last_lat), float(last_lon), lat, lon)
        self.dr_points.append((t, lat, lon, float(result.get('speed', 0)),
                               float(result.get('heading_deg', 0)), drift,
                               len(self.dr_episodes) - 1))

    @property
    def gps_ratio(self):
        return self.gps_valid / self.total * 100 if self.total else 0.0

    def outage_episodes(self):
        """実際のGPS途絶によるDR作動"""
        return [e for e in self.dr_episodes if not e['after_gap']]

    def resume_episodes(self):
        """記録再開直後のDR作動"""
        return [e for e in self.dr_episodes if e['after_gap']]


class TerrainProfileWorker(QThread):
    """標高タイルの取得をバックグラウンドで行うスレッド（ネットワーク待ちでUIを止めない）"""

    profile_ready = Signal(int, list)  # (世代番号, [(distance, elevation), ...])

    def __init__(self, generation, coords, sample_interval, parent=None):
        super().__init__(parent)
        self.generation = generation
        self.coords = coords
        self.sample_interval = sample_interval

    def run(self):
        try:
            gsi_api = GSIElevationAPI(zoom=14)
            profile = gsi_api.get_elevation_profile(
                self.coords, sample_interval=self.sample_interval,
                should_stop=self.isInterruptionRequested)
        except Exception as e:
            print(f'標高タイル取得エラー: {e}')
            profile = []
        if not self.isInterruptionRequested():
            self.profile_ready.emit(self.generation, profile)


class SensorLogViewer(QMainWindow):
    """センサーログビューアのメインウィンドウ"""

    def __init__(self, folder_path=None):
        super().__init__()
        self.setWindowTitle('Sensor Logger Viewer')
        self.setGeometry(100, 100, 1600, 900)

        profile = QWebEngineProfile.defaultProfile()
        if APP_USER_AGENT_SUFFIX not in profile.httpUserAgent():
            profile.setHttpUserAgent(f'{profile.httpUserAgent()} {APP_USER_AGENT_SUFFIX}')

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
        self._stop_terrain_workers()
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
        setup_plot_downsampling(self.gravity_plot)
        motion_layout.addWidget(self.gravity_plot)

        self.accel_plot = pg.PlotWidget(title='User Acceleration (G)')
        self.accel_plot.addLegend()
        self.accel_plot.showGrid(x=True, y=True)
        setup_plot_downsampling(self.accel_plot)
        motion_layout.addWidget(self.accel_plot)

        self.tab_widget.addTab(motion_widget, 'Acceleration')

        # 姿勢タブ
        attitude_widget = QWidget()
        attitude_layout = QVBoxLayout(attitude_widget)

        self.attitude_plot = pg.PlotWidget(title='Attitude (degrees)')
        self.attitude_plot.addLegend()
        self.attitude_plot.showGrid(x=True, y=True)
        setup_plot_downsampling(self.attitude_plot)
        attitude_layout.addWidget(self.attitude_plot)

        self.gyro_plot = pg.PlotWidget(title='Gyroscope (rad/s)')
        self.gyro_plot.addLegend()
        self.gyro_plot.showGrid(x=True, y=True)
        setup_plot_downsampling(self.gyro_plot)
        attitude_layout.addWidget(self.gyro_plot)

        self.tab_widget.addTab(attitude_widget, 'Attitude')

        # 磁場タブ
        magnetic_widget = QWidget()
        magnetic_layout = QVBoxLayout(magnetic_widget)

        self.magnetic_plot = pg.PlotWidget(title='Magnetic Field (μT)')
        self.magnetic_plot.addLegend()
        self.magnetic_plot.showGrid(x=True, y=True)
        setup_plot_downsampling(self.magnetic_plot)
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
        setup_plot_downsampling(self.altitude_plot)
        gps_graphs_layout.addWidget(self.altitude_plot)

        self.speed_plot = pg.PlotWidget(title='Speed (m/s)')
        self.speed_plot.showGrid(x=True, y=True)
        setup_plot_downsampling(self.speed_plot)
        gps_graphs_layout.addWidget(self.speed_plot)

        self.accuracy_plot = pg.PlotWidget(title='GPS Accuracy (m)')
        self.accuracy_plot.showGrid(x=True, y=True)
        setup_plot_downsampling(self.accuracy_plot)
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
        setup_plot_downsampling(self.dr_speed_plot)
        dr_graphs_layout.addWidget(self.dr_speed_plot)

        self.dr_heading_plot = pg.PlotWidget(title='DR Heading (deg)')
        self.dr_heading_plot.showGrid(x=True, y=True)
        setup_plot_downsampling(self.dr_heading_plot)
        dr_graphs_layout.addWidget(self.dr_heading_plot)

        self.dr_error_plot = pg.PlotWidget(title='DR Drift from Last GPS Fix (m)')
        self.dr_error_plot.showGrid(x=True, y=True)
        setup_plot_downsampling(self.dr_error_plot)
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
        # 軸の単位を固定する。自動SI接頭辞に任せると、航空機のログでは標高が km、
        # 距離が Mm に切り替わり、地上のログと数値を見比べられなくなる
        # 距離はデータをメートルのまま持ち、表示だけ km に換算する
        bottom_axis = self.elevation_plot.getAxis('bottom')
        bottom_axis.enableAutoSIPrefix(False)
        bottom_axis.setScale(0.001)
        self.elevation_plot.getAxis('left').enableAutoSIPrefix(False)
        self.elevation_plot.setLabel('bottom', '距離', 'km')
        self.elevation_plot.setLabel('left', '標高', 'm')
        self.elevation_plot.addLegend()
        setup_plot_downsampling(self.elevation_plot)
        elevation_layout.addWidget(self.elevation_plot)

        integrated_splitter.addWidget(elevation_widget)
        integrated_splitter.setSizes([500, 200])  # 地図:断面図 = 5:2

        integrated_layout.addWidget(integrated_splitter)

        self.tab_widget.addTab(integrated_widget, 'Integrated Track')

        # タブ変更時のシグナル接続（遅延読み込み用）
        self._integrated_track_loaded = False
        self._integrated_track_tab_index = self.tab_widget.count() - 1  # 最後に追加したタブ
        self.tab_widget.currentChanged.connect(self._on_tab_changed)

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

        # 3D表示ボタン
        self.view_3d_btn = QPushButton('🌐 Open 3D View in Browser')
        self.view_3d_btn.clicked.connect(self._open_3d_view)
        self.view_3d_btn.setEnabled(False)
        info_layout.addWidget(self.view_3d_btn)

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
            # ファイルサイズを確認
            file_size = Path(file_path).stat().st_size
            file_size_mb = file_size / (1024 * 1024)

            # 大きなファイルでijsonが使用可能な場合はストリーミング読み込み
            use_streaming = (
                IJSON_AVAILABLE and
                file_size_mb > STREAMING_THRESHOLD_MB
            )

            if use_streaming:
                self._load_file_streaming(file_path, file_size_mb)
            else:
                self._load_file_standard(file_path, file_size_mb)

        except Exception as e:
            self.statusBar.showMessage(f'Error loading file: {e}')

    def _load_file_streaming(self, file_path, file_size_mb):
        """ストリーミングでファイルを読み込む（大きなファイル用）"""
        # プログレスダイアログを表示
        progress = QProgressDialog(
            f"Streaming {Path(file_path).name} ({file_size_mb:.1f}MB)...",
            None,
            0, 100,
            self
        )
        progress.setWindowTitle("Loading (Streaming)")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(5)
        QApplication.processEvents()

        # まずメタデータを読み込む
        progress.setLabelText("Reading metadata...")
        progress.setValue(10)
        QApplication.processEvents()

        metadata = {}
        with open(file_path, 'rb') as f:
            # メタデータを取得
            parser = ijson.parse(f)
            for prefix, event, value in parser:
                if prefix.startswith('metadata.'):
                    key = prefix.split('.', 1)[1]
                    if '.' not in key:  # ネストしていないキーのみ
                        metadata[key] = value
                elif prefix == 'record_count':
                    total_records = value
                    break

        # record_countが見つからない場合は推定
        if 'total_records' not in dir():
            # ファイルサイズから概算（1レコード約500バイトと仮定）
            total_records = int(file_size_mb * 1024 * 1024 / 500)

        progress.setLabelText(f"Streaming ~{total_records:,} records...")
        progress.setValue(20)
        QApplication.processEvents()

        # 間引き間隔を計算
        if total_records > MAX_LOAD_RECORDS:
            keep_every = total_records / MAX_LOAD_RECORDS
            self._is_decimated = True
        else:
            keep_every = 1
            self._is_decimated = False

        # レコードをストリーミングで読み込み（間引きながら）
        records = []
        record_index = 0
        next_keep = 0
        last_record = None
        summary = LogSummary()

        with open(file_path, 'rb') as f:
            for record in ijson.items(f, 'records.item'):
                # 最後のレコードを常に保持
                last_record = record
                summary.add(record)

                # 間引き判定
                if record_index >= next_keep:
                    records.append(record)
                    next_keep += keep_every

                    # プログレス更新（100レコードごと）
                    if len(records) % 100 == 0:
                        pct = min(20 + int(60 * record_index / max(total_records, 1)), 80)
                        progress.setValue(pct)
                        progress.setLabelText(
                            f"Loaded {len(records):,} records..."
                        )
                        QApplication.processEvents()

                record_index += 1

        # 最後のレコードを追加（まだ追加されていない場合）
        if last_record and (not records or records[-1] != last_record):
            records.append(last_record)

        self._original_record_count = record_index
        self.records = records
        self._summary = summary

        # log_dataを構築（メタデータのみ）
        self.log_data = {
            'metadata': metadata,
            'record_count': record_index
        }

        self.file_label.setText(Path(file_path).name)
        self._current_file_path = file_path

        # プログレスを渡してプロット
        self._update_metadata()
        self._plot_data(progress)

        # 3Dボタンを有効化（GPSデータがある場合）
        has_gps = any(r.get('gps', {}).get('latitude') for r in self.records)
        self.view_3d_btn.setEnabled(has_gps)

        progress.setValue(100)
        progress.close()

        # ステータス表示
        if self._is_decimated:
            self.statusBar.showMessage(
                f'Streamed {len(self.records):,} records (from {record_index:,}, {file_size_mb:.1f}MB)'
            )
        else:
            self.statusBar.showMessage(f'Streamed {len(self.records):,} records ({file_size_mb:.1f}MB)')

    def _load_file_standard(self, file_path, file_size_mb):
        """標準的なファイル読み込み（小さなファイル用）"""
        show_progress = file_size_mb > 10

        if show_progress:
            progress = QProgressDialog(
                f"Loading {Path(file_path).name} ({file_size_mb:.1f}MB)...",
                None,
                0, 100,
                self
            )
            progress.setWindowTitle("Loading")
            progress.setWindowModality(Qt.WindowModal)
            progress.setMinimumDuration(0)
            progress.setValue(10)
            QApplication.processEvents()
        else:
            progress = None
            self.statusBar.showMessage(f'Loading {file_size_mb:.1f}MB file...')
            QApplication.processEvents()

        # JSON読み込み
        if progress:
            progress.setLabelText("Parsing JSON...")
            progress.setValue(20)
            QApplication.processEvents()

        with open(file_path, 'r', encoding='utf-8') as f:
            self.log_data = json.load(f)

        if progress:
            progress.setValue(50)
            QApplication.processEvents()

        all_records = self.log_data.get('records', [])
        original_count = len(all_records)

        if not all_records:
            if progress:
                progress.close()
            self.statusBar.showMessage('No records found in file')
            return

        # 稀な事象（GPS途絶・DR作動・記録欠落）は間引き前に集計
        self._summary = LogSummary()
        for record in all_records:
            self._summary.add(record)

        # 大きなファイルの場合は読み込み時に間引き
        if progress:
            progress.setLabelText(f"Processing {original_count:,} records...")
            progress.setValue(60)
            QApplication.processEvents()

        if original_count > MAX_LOAD_RECORDS:
            step = original_count / MAX_LOAD_RECORDS
            indices = [int(i * step) for i in range(MAX_LOAD_RECORDS)]
            if indices[-1] != original_count - 1:
                indices.append(original_count - 1)
            self.records = [all_records[i] for i in indices]
            self._original_record_count = original_count
            self._is_decimated = True
            del all_records
        else:
            self.records = all_records
            self._original_record_count = original_count
            self._is_decimated = False

        self.file_label.setText(Path(file_path).name)
        self._current_file_path = file_path
        self._update_metadata()
        self._plot_data(progress)

        # 3Dボタンを有効化（GPSデータがある場合）
        has_gps = any(r.get('gps', {}).get('latitude') for r in self.records)
        self.view_3d_btn.setEnabled(has_gps)

        if progress:
            progress.setValue(100)
            progress.close()

        # ステータス表示
        if self._is_decimated:
            self.statusBar.showMessage(
                f'Loaded {len(self.records):,} records (decimated from {original_count:,}, {file_size_mb:.1f}MB)'
            )
        else:
            self.statusBar.showMessage(f'Loaded {len(self.records):,} records ({file_size_mb:.1f}MB)')

    def _update_metadata(self):
        """メタデータを更新"""
        if not self.log_data:
            return

        metadata = self.log_data.get('metadata', {})
        original_count = getattr(self, '_original_record_count', len(self.records))
        is_decimated = getattr(self, '_is_decimated', False)

        # レコード数の表示（間引き時は元の数も表示）
        if is_decimated:
            records_text = f"{len(self.records):,} / {original_count:,} (decimated)"
        else:
            records_text = f"{original_count:,}"

        meta_text = f"""Session: {metadata.get('session_start', 'N/A')}
Device: {metadata.get('device', 'N/A')}
Version: {metadata.get('app_version', 'N/A')}
Interval: {metadata.get('update_interval_ms', 'N/A')}ms
Records: {records_text}"""

        self.meta_label.setText(meta_text)

        # 統計情報
        if self.records:
            first_time = self.records[0].get('timestamp', 0)
            last_time = self.records[-1].get('timestamp', 0)
            duration = last_time - first_time

            # 元のサンプリングレートを計算
            if is_decimated and duration > 0:
                original_rate = original_count / duration
                display_rate = len(self.records) / duration
                rate_text = f"{display_rate:.1f}Hz (orig: {original_rate:.1f}Hz)"
            else:
                rate_text = f"{len(self.records)/max(duration, 0.1):.1f}Hz"

            stats_text = f"""Duration: {duration:.1f}s ({duration/60:.1f}min)
Avg Rate: {rate_text}
Start: {self.records[0].get('datetime', 'N/A')[:19]}
End: {self.records[-1].get('datetime', 'N/A')[:19]}"""

            self.stats_label.setText(stats_text)

    def _setup_map_combo(self, combo):
        """地図選択コンボボックスをセットアップ"""
        combo.addItem('OpenStreetMap', 'osm')
        combo.addItem('国土地理院（淡色）', 'gsi')
        combo.addItem('国土地理院（標準）', 'gsi_std')
        combo.addItem('国土地理院（写真）', 'gsi_photo')

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

        # リストをnumpy配列に変換（float64を明示的に指定）
        self.gps_lat = np.array(self.gps_lat, dtype=np.float64)
        self.gps_lon = np.array(self.gps_lon, dtype=np.float64)
        self.gps_alt = np.array(self.gps_alt, dtype=np.float64)
        self.gps_speed = np.array(self.gps_speed, dtype=np.float64)
        self.gps_accuracy = np.array(self.gps_accuracy, dtype=np.float64)
        self.gps_time = np.array(self.gps_time, dtype=np.float64)

        self.dr_lat = np.array(self.dr_lat, dtype=np.float64)
        self.dr_lon = np.array(self.dr_lon, dtype=np.float64)
        self.dr_speed = np.array(self.dr_speed, dtype=np.float64)
        self.dr_heading = np.array(self.dr_heading, dtype=np.float64)
        self.dr_time = np.array(self.dr_time, dtype=np.float64)

        # DRは稀にしか作動しないので、間引き後のレコードではなく全レコードの集計を使う
        summary = getattr(self, '_summary', None)
        if summary is not None:
            pts = np.array(summary.dr_points, dtype=np.float64).reshape(-1, 7)
            self.dr_time, self.dr_lat, self.dr_lon, self.dr_speed, self.dr_heading = pts[:, :5].T
            self.dr_drift = pts[:, 5]
            self.dr_episode = pts[:, 6].astype(int)
        else:
            self.dr_drift = np.full(len(self.dr_time), np.nan)
            self.dr_episode = np.zeros(len(self.dr_time), dtype=int)

        # INS軌跡は遅延計算（必要時に計算）
        self._ins_calculated = False
        self.ins_lat = np.array([])
        self.ins_lon = np.array([])

    def _ensure_ins_track(self):
        """INS軌跡が必要な時に計算（遅延計算）"""
        if not self._ins_calculated:
            self._calculate_ins_track()
            self._ins_calculated = True

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

    def _update_progress(self, progress, value, label=None):
        """プログレスダイアログを更新"""
        if progress:
            progress.setValue(value)
            if label:
                progress.setLabelText(label)
            QApplication.processEvents()

    def _plot_data(self, progress=None):
        """データをプロット"""
        self._update_progress(progress, 85, "Extracting sensor data...")
        self._extract_sensor_data()

        self._update_progress(progress, 88, "Plotting sensor graphs...")
        QApplication.processEvents()

        # プロット用の鮮やかな色（ダークテーマ用）
        pen_x = pg.mkPen('#FF6B6B', width=2)  # 鮮やかな赤
        pen_y = pg.mkPen('#4ECB71', width=2)  # 鮮やかな緑
        pen_z = pg.mkPen('#4DABF7', width=2)  # 鮮やかな青

        # 時間軸を一度だけ間引き（センサーデータ共通）
        time_dec, indices = decimate_data(self.time_array)

        # 重力プロット（間引き適用）
        self.gravity_plot.clear()
        self.gravity_plot.addLegend()
        self.gravity_plot.plot(time_dec, self.gravity_x[indices],
                               pen=pen_x, name='X')
        self.gravity_plot.plot(time_dec, self.gravity_y[indices],
                               pen=pen_y, name='Y')
        self.gravity_plot.plot(time_dec, self.gravity_z[indices],
                               pen=pen_z, name='Z')
        self.gravity_plot.setLabel('bottom', 'Time', 's')
        QApplication.processEvents()

        # 加速度プロット（間引き適用）
        self.accel_plot.clear()
        self.accel_plot.addLegend()
        self.accel_plot.plot(time_dec, self.accel_x[indices],
                             pen=pen_x, name='X')
        self.accel_plot.plot(time_dec, self.accel_y[indices],
                             pen=pen_y, name='Y')
        self.accel_plot.plot(time_dec, self.accel_z[indices],
                             pen=pen_z, name='Z')
        self.accel_plot.setLabel('bottom', 'Time', 's')
        QApplication.processEvents()

        # 姿勢プロット（間引き適用）
        pen_roll = pg.mkPen('#FF8787', width=2)   # ピンク系赤
        pen_pitch = pg.mkPen('#69DB7C', width=2)  # 明るい緑
        pen_yaw = pg.mkPen('#74C0FC', width=2)    # 明るい青

        self.attitude_plot.clear()
        self.attitude_plot.addLegend()
        self.attitude_plot.plot(time_dec, self.roll[indices],
                                pen=pen_roll, name='Roll')
        self.attitude_plot.plot(time_dec, self.pitch[indices],
                                pen=pen_pitch, name='Pitch')
        self.attitude_plot.plot(time_dec, self.yaw[indices],
                                pen=pen_yaw, name='Yaw')
        self.attitude_plot.setLabel('bottom', 'Time', 's')
        QApplication.processEvents()

        # ジャイロプロット（間引き適用）
        self.gyro_plot.clear()
        self.gyro_plot.addLegend()
        self.gyro_plot.plot(time_dec, self.gyro_x[indices],
                            pen=pen_x, name='X')
        self.gyro_plot.plot(time_dec, self.gyro_y[indices],
                            pen=pen_y, name='Y')
        self.gyro_plot.plot(time_dec, self.gyro_z[indices],
                            pen=pen_z, name='Z')
        self.gyro_plot.setLabel('bottom', 'Time', 's')
        QApplication.processEvents()

        # 磁場プロット（間引き適用）
        self.magnetic_plot.clear()
        self.magnetic_plot.addLegend()
        self.magnetic_plot.plot(time_dec, self.mag_x[indices],
                                pen=pen_x, name='X')
        self.magnetic_plot.plot(time_dec, self.mag_y[indices],
                                pen=pen_y, name='Y')
        self.magnetic_plot.plot(time_dec, self.mag_z[indices],
                                pen=pen_z, name='Z')
        self.magnetic_plot.setLabel('bottom', 'Time', 's')
        QApplication.processEvents()

        self._update_progress(progress, 90, "Plotting GPS track...")
        QApplication.processEvents()

        # GPSプロット
        self._plot_gps()

        self._update_progress(progress, 95, "Plotting dead reckoning...")

        # デッドレコニングプロット
        self._plot_dead_reckoning()

        self._update_progress(progress, 98, "Finalizing...")
        QApplication.processEvents()

        # 統合航跡は遅延読み込み（タブ選択時に計算）
        self._integrated_track_loaded = False

    def _on_tab_changed(self, index):
        """タブ変更時の処理（遅延読み込み）"""
        if index == self._integrated_track_tab_index and not self._integrated_track_loaded:
            if len(self.records) > 0:
                # ステータスバーに表示
                self.statusBar.showMessage('Loading integrated track...')
                QApplication.processEvents()

                # 統合航跡を計算
                self._plot_integrated_track()
                self._integrated_track_loaded = True

                self.statusBar.showMessage('Integrated track loaded', 3000)

    def _plot_gps(self):
        """GPSデータをプロット"""
        self.altitude_plot.clear()
        self.speed_plot.clear()
        self.accuracy_plot.clear()

        if len(self.gps_lat) == 0:
            self.gps_info_label.setText('No GPS data')
            return

        # 地図に軌跡を表示（初期表示用に間引き）
        # 大量データの場合は間引いてから変換（高速化）
        n_gps = len(self.gps_lat)
        if n_gps > MAP_MAX_POINTS:
            step = n_gps // MAP_MAX_POINTS
            gps_indices = list(range(0, n_gps, step))
            if gps_indices[-1] != n_gps - 1:
                gps_indices.append(n_gps - 1)
            gps_data = [
                {'lat': float(self.gps_lat[i]), 'lon': float(self.gps_lon[i]), 'accuracy': float(self.gps_accuracy[i])}
                for i in gps_indices
            ]
        else:
            gps_data = [
                {'lat': float(lat), 'lon': float(lon), 'accuracy': float(acc)}
                for lat, lon, acc in zip(self.gps_lat, self.gps_lon, self.gps_accuracy)
            ]

        dr_coords = self._dr_map_coords()

        # INS軌跡は重いので初期表示では省略（必要に応じて後で計算）
        ins_coords = []

        # 地図へのJS実行は最後に移動（テスト）

        # グラフ（間引き適用）
        gps_time_dec, gps_alt_dec = decimate_xy(self.gps_time, self.gps_alt)
        self.altitude_plot.plot(gps_time_dec, gps_alt_dec, pen=pg.mkPen('#5CD8FF', width=2))
        self.altitude_plot.setLabel('bottom', 'Time', 's')

        gps_time_dec, gps_speed_dec = decimate_xy(self.gps_time, self.gps_speed)
        self.speed_plot.plot(gps_time_dec, gps_speed_dec, pen=pg.mkPen('#69DB7C', width=2))
        self.speed_plot.setLabel('bottom', 'Time', 's')

        gps_time_dec, gps_acc_dec = decimate_xy(self.gps_time, self.gps_accuracy)
        self.accuracy_plot.plot(gps_time_dec, gps_acc_dec, pen=pg.mkPen('#FFA94D', width=2))
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

        # 地図へのJS実行
        js = f'setGPSTrackWithAccuracy({json.dumps(gps_data)}, {json.dumps(dr_coords)}, {json.dumps(ins_coords)});'
        self.map_view.page().runJavaScript(js)

    def _plot_dead_reckoning(self):
        """デッドレコニングデータをプロット"""
        self.dr_speed_plot.clear()
        self.dr_speed_plot.addLegend()
        self.dr_heading_plot.clear()
        self.dr_error_plot.clear()

        if len(self.gps_lat) == 0:
            self.dr_info_label.setText('No GPS data')
            return

        # DR地図（初期表示用に間引き - _plot_gps()と同様の最適化）
        n_gps = len(self.gps_lat)
        if n_gps > MAP_MAX_POINTS:
            step = n_gps // MAP_MAX_POINTS
            gps_indices = list(range(0, n_gps, step))
            if gps_indices[-1] != n_gps - 1:
                gps_indices.append(n_gps - 1)
            gps_data = [
                {'lat': float(self.gps_lat[i]), 'lon': float(self.gps_lon[i]), 'accuracy': float(self.gps_accuracy[i])}
                for i in gps_indices
            ]
        else:
            gps_data = [
                {'lat': float(lat), 'lon': float(lon), 'accuracy': float(acc)}
                for lat, lon, acc in zip(self.gps_lat, self.gps_lon, self.gps_accuracy)
            ]

        dr_coords = self._dr_map_coords()

        # INS軌跡は重いので初期表示では省略
        ins_coords = []

        js = f'setGPSTrackWithAccuracy({json.dumps(gps_data)}, {json.dumps(dr_coords)}, {json.dumps(ins_coords)});'
        self.dr_map_view.page().runJavaScript(js)

        # GPS速度も表示（間引き適用、float64に変換）
        if len(self.gps_time) > 0:
            gps_time_f = np.asarray(self.gps_time, dtype=np.float64)
            gps_speed_f = np.asarray(self.gps_speed, dtype=np.float64)
            gps_time_dec, gps_speed_dec = decimate_xy(gps_time_f, gps_speed_f)
            self.dr_speed_plot.plot(gps_time_dec, gps_speed_dec,
                                    pen=pg.mkPen('#4DABF7', width=2),
                                    name='GPS')

        # 記録欠落区間を背景に示す（DRが作動した理由を読み取れるようにする）
        summary = getattr(self, '_summary', None)
        if summary is not None:
            for start, dur in summary.gaps[:500]:
                region = pg.LinearRegionItem(values=(start, start + dur), movable=False,
                                             brush=pg.mkBrush(255, 255, 255, 40),
                                             pen=pg.mkPen(None))
                region.setZValue(-10)
                self.dr_speed_plot.addItem(region)
        self.dr_speed_plot.setLabel('bottom', 'Time', 's')

        self.dr_info_label.setText(self._dead_reckoning_summary_text())

        if len(self.dr_lat) == 0:
            return

        # 作動区間ごとに線を切る（区間をまたいで結ぶと実在しない速度変化が描かれる）
        def split_by_episode(values):
            breaks = np.flatnonzero(np.diff(self.dr_episode) != 0) + 1
            t = np.insert(self.dr_time, breaks, np.nan)
            v = np.insert(np.asarray(values, dtype=np.float64), breaks, np.nan)
            return t, v

        dr_pen = pg.mkPen('#DA77F2', width=2)
        dr_symbol = dict(symbol='o', symbolSize=6, symbolBrush='#DA77F2', symbolPen=None)

        t, v = split_by_episode(self.dr_speed)
        self.dr_speed_plot.plot(t, v, pen=dr_pen, connect='finite', name='DR', **dr_symbol)

        t, v = split_by_episode(self.dr_heading)
        self.dr_heading_plot.plot(t, v, pen=dr_pen, connect='finite', **dr_symbol)
        self.dr_heading_plot.setLabel('bottom', 'Time', 's')

        # 最終GPS測位点からの推測位置の隔たり（DRの誤差の上限の目安）
        t, v = split_by_episode(self.dr_drift)
        self.dr_error_plot.plot(t, v, pen=dr_pen, connect='finite', **dr_symbol)
        self.dr_error_plot.setLabel('bottom', 'Time', 's')

    def _dr_map_coords(self):
        """
        地図に描くDR座標。実際のGPS途絶で作動したものだけを返す
        （記録再開直後の単発の作動点を結ぶと、実在しない直線が引かれる）
        """
        if len(self.dr_lat) == 0:
            return []
        dr_mask = np.ones(len(self.dr_lat), dtype=bool)
        summary = getattr(self, '_summary', None)
        if summary is not None:
            outage = np.array([not e['after_gap'] for e in summary.dr_episodes], dtype=bool)
            dr_mask = outage[self.dr_episode]
        dr_idx = np.flatnonzero(dr_mask)
        if len(dr_idx) > MAP_MAX_POINTS:
            dr_idx = dr_idx[::len(dr_idx) // MAP_MAX_POINTS]
        return [[float(self.dr_lat[i]), float(self.dr_lon[i])] for i in dr_idx]

    def _dead_reckoning_summary_text(self):
        """DRの作動状況を、GPSの可用性と記録欠落に照らして要約する"""
        summary = getattr(self, '_summary', None)
        if summary is None or summary.total == 0:
            return 'No data'

        outage = summary.outage_episodes()
        resume = summary.resume_episodes()
        outage_sec = sum(e['end'] - e['start'] for e in outage)
        gap_total = sum(d for _, d in summary.gaps)
        gap_max = max((d for _, d in summary.gaps), default=0.0)

        lines = [
            f'GPS Availability: {summary.gps_ratio:.2f}%',
            f'  ({summary.gps_valid:,} / {summary.total:,} records)',
            f'Recording Gaps (>{LogSummary.GAP_THRESHOLD_S:.0f}s): {len(summary.gaps)}',
            f'  total {gap_total:.0f}s, longest {gap_max:.0f}s',
            f'DR Activations: {len(summary.dr_episodes)}',
            f'  GPS outage: {len(outage)} ({outage_sec:.1f}s)',
            f'  after recording resume: {len(resume)}',
        ]
        if outage:
            outage_idx = {i for i, e in enumerate(summary.dr_episodes) if not e['after_gap']}
            drift = [p[5] for p in summary.dr_points
                     if p[6] in outage_idx and not math.isnan(p[5])]
            if drift:
                lines.append(f'Max Drift from Last Fix: {max(drift):.1f}m')
        else:
            lines.append('No GPS outage: DR was not needed.')
        return '\n'.join(lines)

    def _plot_integrated_track(self):
        """統合航跡を計算してプロット"""
        if len(self.records) == 0:
            return

        # 大量レコードの場合は間引いて処理（高速化）
        records_to_process = self.records
        n_records = len(self.records)
        if n_records > MAX_DISPLAY_POINTS:
            step = n_records // MAX_DISPLAY_POINTS
            indices = list(range(0, n_records, step))
            if indices[-1] != n_records - 1:
                indices.append(n_records - 1)
            records_to_process = [self.records[i] for i in indices]

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

        for rec in records_to_process:
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
                    accuracy = float(raw.get('horizontal_accuracy', 100))
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
                accuracy = float(raw.get('horizontal_accuracy', 100))
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
                lat = float(lat)
                lon = float(lon)
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

        # セグメントを描画（JavaScript側で動的間引きを行う）
        for coords, track_type in segments:
            if len(coords) >= 2:
                js = f'addTrackSegment({json.dumps(coords)}, "{track_type}");'
                self.integrated_map_view.page().runJavaScript(js)

        # 全セグメント追加後に描画を実行
        self.integrated_map_view.page().runJavaScript('finishAddingSegments();')

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

            lat = float(lat)
            lon = float(lon)

            # 距離計算
            if prev_lat is not None:
                dist = self._haversine_distance(prev_lat, prev_lon, lat, lon)
                total_distance += dist

            # dt計算
            dt = 0.1
            if prev_time is not None:
                dt = float(timestamp - prev_time)
            prev_time = timestamp

            # GPS高度
            gps_alt = None
            gps_v_acc = None
            if raw and not gps.get('no_signal', True):
                gps_alt = raw.get('altitude')
                gps_v_acc = raw.get('vertical_accuracy', -1)

                if gps_alt is not None and gps_alt != 0:
                    gps_distances.append(total_distance)
                    gps_altitudes.append(float(gps_alt))

            # 気圧計相対高度
            baro_relative = None
            if baro:
                baro_relative = baro.get('relative_altitude_m')
                if baro_relative is not None:
                    baro_relative = float(baro_relative)
                    has_barometer_data = True

            # 加速度Z軸
            accel_z = None
            if accel:
                accel_z = accel.get('z')
                if accel_z is not None:
                    accel_z = float(accel_z)

            # GPS高度もfloat変換
            gps_alt_f = float(gps_alt) if gps_alt is not None else None
            gps_v_acc_f = float(gps_v_acc) if gps_v_acc is not None else None

            # 高度融合（気圧計データがある場合のみ記録）
            fused_alt = altitude_fusion.update(gps_alt_f, gps_v_acc_f, baro_relative, accel_z, dt)
            if fused_alt is not None and baro_relative is not None:
                fused_distances.append(total_distance)
                fused_altitudes.append(fused_alt)

            # 距離→座標の対応を記録
            self._distance_to_coord.append((total_distance, lat, lon))

            prev_lat, prev_lon = lat, lon

        # 国土地理院標高タイルから地形断面を取得（バックグラウンド、提供範囲内の航跡のみ）
        self._start_terrain_worker(coords)

        # GPS高度をプロット（地図のGPS Good色と統一: #118ab2）
        if gps_distances and gps_altitudes:
            gps_dist_arr = np.array(gps_distances, dtype=np.float64)
            gps_alt_arr = np.array(gps_altitudes, dtype=np.float64)
            self.elevation_plot.plot(gps_dist_arr, gps_alt_arr,
                                     pen=pg.mkPen('#118ab2', width=2),
                                     name='GPS高度')

        # 融合高度をプロット（気圧計データがある場合のみ、地図のFusion色と統一: #00CED1）
        if fused_distances and fused_altitudes and has_barometer_data:
            fused_dist_arr = np.array(fused_distances, dtype=np.float64)
            fused_alt_arr = np.array(fused_altitudes, dtype=np.float64)
            self.elevation_plot.plot(fused_dist_arr, fused_alt_arr,
                                     pen=pg.mkPen('#00CED1', width=2),
                                     name='融合高度(GPS+気圧計)')

        self._distance_array = np.array([d for d, _, _ in self._distance_to_coord], dtype=np.float64)

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

    def _start_terrain_worker(self, coords):
        """地形断面の取得をバックグラウンドで開始（前回分は破棄）"""
        self._terrain_generation = getattr(self, '_terrain_generation', 0) + 1
        self._stop_terrain_workers(wait=False)

        # 提供範囲外（海外など）はタイルが存在しないので問い合わせない
        if not any(GSIElevationAPI.in_coverage(lat, lon) for lat, lon in coords):
            return

        # サンプリング間隔を調整（データ量に応じて）
        sample_interval = max(1, len(coords) // 100)
        worker = TerrainProfileWorker(self._terrain_generation, coords, sample_interval, self)
        worker.profile_ready.connect(self._on_terrain_profile_ready)
        worker.finished.connect(lambda w=worker: self._on_terrain_worker_finished(w))
        self._terrain_workers.append(worker)
        worker.start()

    def _on_terrain_worker_finished(self, worker):
        if worker in self._terrain_workers:
            self._terrain_workers.remove(worker)
        worker.deleteLater()

    def _stop_terrain_workers(self, wait=True):
        """実行中の地形断面取得を中断"""
        if not hasattr(self, '_terrain_workers'):
            self._terrain_workers = []
        for worker in self._terrain_workers:
            worker.requestInterruption()
            if wait:
                worker.wait(6000)  # urlopen のタイムアウト(5秒)より長く待つ

    def _on_terrain_profile_ready(self, generation, terrain_profile):
        """地形断面の取得完了時に描画（古い世代の結果は捨てる）"""
        if generation != self._terrain_generation or not terrain_profile:
            return

        terrain_dist = [p[0] for p in terrain_profile]
        terrain_elev = [p[1] for p in terrain_profile]

        # 地形断面を塗りつぶしで描画（高度の線より背面に置く）
        terrain_brush = pg.mkBrush('#4a5568')
        fill = pg.FillBetweenItem(
            pg.PlotDataItem(terrain_dist, terrain_elev),
            pg.PlotDataItem(terrain_dist, [min(terrain_elev) - 10] * len(terrain_dist)),
            brush=terrain_brush
        )
        fill.setZValue(-20)
        self.elevation_plot.addItem(fill)

        # 地形の線も描画
        line = self.elevation_plot.plot(terrain_dist, terrain_elev,
                                        pen=pg.mkPen('#718096', width=2),
                                        name='地形標高')
        line.setZValue(-10)

    def _on_elevation_region_changed(self):
        """断面図の区間選択が変更された時のコールバック"""
        if not hasattr(self, '_elevation_region') or not getattr(self, '_distance_to_coord', None):
            return

        region = self._elevation_region.getRegion()
        start_dist, end_dist = region

        # 距離から座標を二分探索（ドラッグ中に毎回呼ばれるため線形走査しない）
        dists = self._distance_array
        i_start = int(np.searchsorted(dists, start_dist, side='left'))
        i_end = int(np.searchsorted(dists, end_dist, side='right')) - 1

        start_coord = None
        end_coord = None
        if i_start < len(dists):
            start_coord = self._distance_to_coord[i_start][1:]
        if i_end >= 0:
            end_coord = self._distance_to_coord[i_end][1:]

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

    def _open_3d_view(self):
        """3Dビューをブラウザで開く"""
        if not self.records:
            return

        # GPSデータを抽出
        track_points = []
        for record in self.records:
            gps = record.get('gps', {})
            lat = gps.get('latitude')
            lon = gps.get('longitude')
            alt = gps.get('altitude', 0)

            if lat is not None and lon is not None:
                track_points.append({
                    'lat': lat,
                    'lon': lon,
                    'alt': alt if alt is not None else 0
                })

        if not track_points:
            return

        # 3D表示用に間引き（ブラウザのパフォーマンス考慮）
        track_points = decimate_coords(track_points, MAP_MAX_POINTS)
        original_points = len(self.records)

        # 中心座標を計算
        center_lat = sum(p['lat'] for p in track_points) / len(track_points)
        center_lon = sum(p['lon'] for p in track_points) / len(track_points)

        # 高度の範囲
        min_alt = min(p['alt'] for p in track_points)
        max_alt = max(p['alt'] for p in track_points)
        alt_range = max_alt - min_alt if max_alt > min_alt else 1

        # ファイル名を取得
        file_name = Path(self._current_file_path).stem if hasattr(self, '_current_file_path') else 'Flight Track'

        # HTML生成
        html_content = f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>3D Flight Track - {file_name}</title>
    <style>
        body {{ margin: 0; overflow: hidden; font-family: Arial, sans-serif; }}
        #info {{
            position: absolute;
            top: 10px;
            left: 10px;
            background: rgba(0,0,0,0.7);
            color: white;
            padding: 15px;
            border-radius: 8px;
            font-size: 14px;
            z-index: 100;
        }}
        #info h2 {{ margin: 0 0 10px 0; font-size: 16px; }}
        #info p {{ margin: 5px 0; }}
        #controls {{
            position: absolute;
            bottom: 10px;
            left: 10px;
            background: rgba(0,0,0,0.7);
            color: white;
            padding: 10px;
            border-radius: 8px;
            font-size: 12px;
        }}
        #altitude-bar {{
            position: absolute;
            right: 20px;
            top: 50%;
            transform: translateY(-50%);
            width: 30px;
            height: 300px;
            background: linear-gradient(to top, #00ff00, #ffff00, #ff0000);
            border-radius: 5px;
            border: 2px solid white;
        }}
        #altitude-labels {{
            position: absolute;
            right: 60px;
            top: 50%;
            transform: translateY(-50%);
            height: 300px;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            color: white;
            font-size: 12px;
            text-shadow: 1px 1px 2px black;
        }}
    </style>
</head>
<body>
    <div id="info">
        <h2>🛩️ {file_name}</h2>
        <p>📍 Points: {len(track_points)}</p>
        <p>📏 Alt Range: {min_alt:.0f}m - {max_alt:.0f}m</p>
        <p>📊 Center: {center_lat:.4f}, {center_lon:.4f}</p>
    </div>
    <div id="controls">
        🖱️ Drag: Rotate | Scroll: Zoom | Right-drag: Pan
    </div>
    <div id="altitude-bar"></div>
    <div id="altitude-labels">
        <span>{max_alt:.0f}m</span>
        <span>{(max_alt + min_alt) / 2:.0f}m</span>
        <span>{min_alt:.0f}m</span>
    </div>

    <script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js"></script>
    <script>
        // トラックデータ
        const trackData = {json.dumps(track_points)};
        const centerLat = {center_lat};
        const centerLon = {center_lon};
        const minAlt = {min_alt};
        const maxAlt = {max_alt};
        const altRange = {alt_range};

        // シーン設定
        const scene = new THREE.Scene();
        scene.background = new THREE.Color(0x1a1a2e);

        // カメラ
        const camera = new THREE.PerspectiveCamera(60, window.innerWidth / window.innerHeight, 0.1, 10000);
        camera.position.set(500, 400, 500);

        // レンダラー
        const renderer = new THREE.WebGLRenderer({{ antialias: true }});
        renderer.setSize(window.innerWidth, window.innerHeight);
        renderer.setPixelRatio(window.devicePixelRatio);
        document.body.appendChild(renderer.domElement);

        // コントロール
        const controls = new THREE.OrbitControls(camera, renderer.domElement);
        controls.enableDamping = true;
        controls.dampingFactor = 0.05;

        // 照明
        const ambientLight = new THREE.AmbientLight(0xffffff, 0.6);
        scene.add(ambientLight);
        const directionalLight = new THREE.DirectionalLight(0xffffff, 0.8);
        directionalLight.position.set(100, 200, 100);
        scene.add(directionalLight);

        // 座標変換（緯度経度をXZ平面に、高度をYに）
        const scale = 10000; // スケール係数
        const altScale = 1;  // 高度スケール

        function latLonToXZ(lat, lon) {{
            const x = (lon - centerLon) * scale * Math.cos(centerLat * Math.PI / 180);
            const z = -(lat - centerLat) * scale;
            return {{ x, z }};
        }}

        // グリッド
        const gridSize = 1000;
        const gridHelper = new THREE.GridHelper(gridSize, 20, 0x444466, 0x333355);
        scene.add(gridHelper);

        // 軸ヘルパー
        const axesHelper = new THREE.AxesHelper(100);
        scene.add(axesHelper);

        // 飛行経路の頂点を作成
        const points = [];
        const colors = [];

        trackData.forEach(point => {{
            const {{ x, z }} = latLonToXZ(point.lat, point.lon);
            const y = (point.alt - minAlt) * altScale;
            points.push(new THREE.Vector3(x, y, z));

            // 高度に基づく色（緑→黄→赤）
            const t = altRange > 0 ? (point.alt - minAlt) / altRange : 0;
            const color = new THREE.Color();
            if (t < 0.5) {{
                color.setRGB(t * 2, 1, 0); // 緑→黄
            }} else {{
                color.setRGB(1, 2 - t * 2, 0); // 黄→赤
            }}
            colors.push(color.r, color.g, color.b);
        }});

        // ライン描画
        const geometry = new THREE.BufferGeometry().setFromPoints(points);
        geometry.setAttribute('color', new THREE.Float32BufferAttribute(colors, 3));

        const material = new THREE.LineBasicMaterial({{
            vertexColors: true,
            linewidth: 2
        }});

        const line = new THREE.Line(geometry, material);
        scene.add(line);

        // 始点と終点のマーカー
        const sphereGeom = new THREE.SphereGeometry(5, 16, 16);

        // 始点（緑）
        const startMat = new THREE.MeshLambertMaterial({{ color: 0x00ff00 }});
        const startSphere = new THREE.Mesh(sphereGeom, startMat);
        startSphere.position.copy(points[0]);
        scene.add(startSphere);

        // 終点（赤）
        const endMat = new THREE.MeshLambertMaterial({{ color: 0xff0000 }});
        const endSphere = new THREE.Mesh(sphereGeom, endMat);
        endSphere.position.copy(points[points.length - 1]);
        scene.add(endSphere);

        // 地表面（半透明）
        const groundGeom = new THREE.PlaneGeometry(gridSize, gridSize);
        const groundMat = new THREE.MeshLambertMaterial({{
            color: 0x2d4a3e,
            transparent: true,
            opacity: 0.5,
            side: THREE.DoubleSide
        }});
        const ground = new THREE.Mesh(groundGeom, groundMat);
        ground.rotation.x = -Math.PI / 2;
        ground.position.y = -1;
        scene.add(ground);

        // カメラを軌跡の中心に向ける
        if (points.length > 0) {{
            const center = new THREE.Vector3();
            points.forEach(p => center.add(p));
            center.divideScalar(points.length);
            controls.target.copy(center);
        }}

        // アニメーション
        function animate() {{
            requestAnimationFrame(animate);
            controls.update();
            renderer.render(scene, camera);
        }}
        animate();

        // リサイズ対応
        window.addEventListener('resize', () => {{
            camera.aspect = window.innerWidth / window.innerHeight;
            camera.updateProjectionMatrix();
            renderer.setSize(window.innerWidth, window.innerHeight);
        }});
    </script>
</body>
</html>'''

        # 一時ファイルに保存してブラウザで開く
        with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8') as f:
            f.write(html_content)
            temp_path = f.name

        webbrowser.open(f'file://{temp_path}')


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
