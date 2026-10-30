/*
 * UPS status row for the Proxmox VE node summary (PVE 9).
 *
 * Ups-nut-bridge rewrites /pve2/js/ups-status.js every ~10 s; this widget
 * polls that script every second, so the row follows the bridge within a
 * second of each new frame.  A script tag is used instead of the API so the
 * PVE backend needs no extra endpoint.
 *
 * Each frame also carries the last 30 minutes of samples (history), which
 * lets the battery gauge and the two sparklines stay dependency-free.
 *
 * Installed to /usr/share/pve-manager/js/pve-ups-status.js and loaded from
 * index.html.tpl.  patch-pve-gui.sh re-applies both after pve-manager
 * upgrades.
 */
(function () {
    'use strict';

    if (
        typeof Ext === 'undefined' ||
        typeof PVE === 'undefined' ||
        !PVE.node ||
        !PVE.node.StatusView
    ) {
        return;
    }

    var REFRESH_MS = 1000;
    var DATA_URL = '/pve2/js/ups-status.js';
    var CHART_WIDTH = 240;
    var CHART_HEIGHT = 36;
    var VOLTAGE_MIN = 21.0;
    var VOLTAGE_MAX = 28.0;
    // Fallback only: the bridge ships the calibrated threshold in the frame.
    var VOLTAGE_WARN = 23.2;
    var instances = [];

    window.__PVE_UPS_STATUS__ = function (data) {
        instances.forEach(function (widget) {
            try {
                widget.setUpsData(data);
            } catch (err) {
                if (window.console) {
                    console.error('UPS status update failed', err);
                }
            }
        });
    };

    function escapeHtml(value) {
        return Ext.util.Format.htmlEncode(String(value));
    }

    function numberText(value, digits, suffix) {
        if (!Ext.isNumber(value)) {
            return '—';
        }
        return value.toFixed(digits) + suffix;
    }

    function runtimeText(seconds) {
        if (!Ext.isNumber(seconds) || seconds <= 0) {
            return '—';
        }
        var total = Math.round(seconds / 60);
        var hours = Math.floor(total / 60);
        var minutes = total % 60;
        if (hours <= 0) {
            return minutes + ' 分钟';
        }
        if (minutes === 0) {
            return hours + ' 小时';
        }
        return hours + ' 小时 ' + minutes + ' 分';
    }

    function stateInfo(data) {
        var status = (data && data.status) || '';
        if (data && data.stale) {
            return {
                label: '通信中断',
                color: '#e05d5d',
                bg: 'rgba(224,93,93,0.16)',
            };
        }
        if (status.indexOf('FSD') !== -1) {
            return {
                label: '正在关机',
                color: '#e05d5d',
                bg: 'rgba(224,93,93,0.16)',
            };
        }
        if (status.indexOf('LB') !== -1) {
            return {
                label: '电池电量低',
                color: '#e05d5d',
                bg: 'rgba(224,93,93,0.16)',
            };
        }
        if (status.indexOf('OB') !== -1) {
            return {
                label: '电池供电',
                color: '#e5a13d',
                bg: 'rgba(229,161,61,0.16)',
            };
        }
        if (data && data.self_test) {
            // Mains is present, but the inverter carries the load, so the
            // battery really is draining during the test.
            return {
                label: '自检中',
                color: '#4d9de0',
                bg: 'rgba(77,157,224,0.16)',
            };
        }
        if (status.indexOf('OL') !== -1) {
            return {
                label: '市电正常',
                color: '#4caf50',
                bg: 'rgba(76,175,80,0.14)',
            };
        }
        return {
            label: status || '未知',
            color: '#999999',
            bg: 'rgba(128,128,128,0.12)',
        };
    }

    function batterySvg(charge, color) {
        var percent = Ext.isNumber(charge) ? Math.max(0, Math.min(100, charge)) : 0;
        var fill = 45 * percent / 100;
        return (
            '<svg width="58" height="26" viewBox="0 0 58 26" style="vertical-align:middle;flex:0 0 auto">' +
            '<rect x="1.25" y="1.25" width="49.5" height="23.5" rx="3" fill="none" stroke="' +
            color +
            '" stroke-opacity="0.55" stroke-width="1.5"/>' +
            '<rect x="53" y="8.5" width="3.5" height="9" rx="1.5" fill="' +
            color +
            '" fill-opacity="0.55"/>' +
            (percent > 0
                ? '<rect x="3.5" y="3.5" width="' +
                  fill.toFixed(1) +
                  '" height="19" rx="1.5" fill="' +
                  color +
                  '" fill-opacity="0.85"/>'
                : '') +
            '</svg>'
        );
    }

    function emptyChart() {
        return (
            '<div style="box-sizing:border-box;height:' +
            CHART_HEIGHT +
            'px;line-height:' +
            CHART_HEIGHT +
            'px;text-align:center;font-size:11px;opacity:0.4;border:1px dashed rgba(128,128,128,0.35);border-radius:4px">采集中…</div>'
        );
    }

    function loadCeiling(history) {
        var peak = 0;
        if (Ext.isArray(history)) {
            for (var i = 0; i < history.length; i++) {
                var row = history[i];
                if (row && Ext.isNumber(row[2])) {
                    peak = Math.max(peak, row[2]);
                }
            }
        }
        if (peak < 30) {
            return 30;
        }
        return Math.min(100, Math.ceil((peak + 5) / 10) * 10);
    }

    function sparkline(history, index, minValue, maxValue, color, warnValue) {
        var values = [];
        if (Ext.isArray(history)) {
            for (var i = 0; i < history.length; i++) {
                var row = history[i];
                if (row && Ext.isNumber(row[index])) {
                    values.push(row[index]);
                }
            }
        }
        if (values.length < 2) {
            return emptyChart();
        }

        var lo = Ext.isNumber(minValue) ? minValue : Math.min.apply(null, values);
        var hi = Ext.isNumber(maxValue) ? maxValue : Math.max.apply(null, values);
        if (lo === hi) {
            lo -= 1;
            hi += 1;
        }
        if (hi < lo) {
            var swap = hi;
            hi = lo;
            lo = swap;
        }

        var padX = 2;
        var padY = 4;
        var span = hi - lo;
        var step = (CHART_WIDTH - 2 * padX) / (values.length - 1);
        var points = [];
        for (i = 0; i < values.length; i++) {
            var x = padX + i * step;
            var y = padY + (CHART_HEIGHT - 2 * padY) * (1 - (values[i] - lo) / span);
            y = Math.max(padY, Math.min(CHART_HEIGHT - padY, y));
            points.push(x.toFixed(1) + ',' + y.toFixed(1));
        }

        var line = 'M' + points.join(' L');
        var area =
            line +
            ' L' +
            (CHART_WIDTH - padX).toFixed(1) +
            ',' +
            (CHART_HEIGHT - padY) +
            ' L' +
            padX.toFixed(1) +
            ',' +
            (CHART_HEIGHT - padY) +
            ' Z';

        var warn = '';
        if (Ext.isNumber(warnValue) && warnValue > lo && warnValue < hi) {
            var warnY =
                padY +
                (CHART_HEIGHT - 2 * padY) * (1 - (warnValue - lo) / span);
            warn =
                '<line x1="' +
                padX +
                '" y1="' +
                warnY.toFixed(1) +
                '" x2="' +
                (CHART_WIDTH - padX) +
                '" y2="' +
                warnY.toFixed(1) +
                '" stroke="#e05d5d" stroke-width="1" stroke-dasharray="3 3" opacity="0.35"/>';
        }

        return (
            '<svg viewBox="0 0 ' +
            CHART_WIDTH +
            ' ' +
            CHART_HEIGHT +
            '" width="100%" height="' +
            CHART_HEIGHT +
            '" preserveAspectRatio="none" style="display:block">' +
            '<path d="' +
            area +
            '" fill="' +
            color +
            '" opacity="0.12"/>' +
            warn +
            '<path d="' +
            line +
            '" fill="none" stroke="' +
            color +
            '" stroke-width="1.6" vector-effect="non-scaling-stroke"/>' +
            '</svg>'
        );
    }

    function render(data) {
        if (!data || !Ext.isNumber(data.ts)) {
            return (
                '<div style="display:flex;align-items:center;gap:14px;min-height:72px">' +
                '<i class="fa fa-bolt" style="font-size:22px;opacity:0.35"></i>' +
                '<div>' +
                '<div style="line-height:1.8"><b>UPS</b>' +
                '<span style="opacity:0.6;margin-left:12px">暂无数据</span></div>' +
                '<div style="font-size:12px;opacity:0.5">等待桥接程序写入 /pve2/js/ups-status.js</div>' +
                '</div>' +
                '</div>'
            );
        }

        var info = stateInfo(data);
        var charge = Ext.isNumber(data.charge)
            ? Math.max(0, Math.min(100, data.charge))
            : null;
        var levelColor = '#999999';
        if (charge !== null) {
            levelColor = charge >= 50 ? '#4caf50' : charge >= 20 ? '#e5a13d' : '#e05d5d';
        }
        var updated = Ext.Date.format(new Date(data.ts * 1000), 'H:i:s');
        var stale = !data.stale && Date.now() / 1000 - data.ts > 120;
        var history = Ext.isArray(data.history) ? data.history : [];
        var warnVoltage = Ext.isNumber(data.warn_voltage)
            ? data.warn_voltage
            : VOLTAGE_WARN;

        var details = [];
        if (Ext.isNumber(data.input_voltage)) {
            details.push('输入 ' + numberText(data.input_voltage, 0, ' V'));
        }
        if (Ext.isNumber(data.temperature)) {
            details.push('温度 ' + numberText(data.temperature, 1, ' °C'));
        }
        if (data.status) {
            details.push(escapeHtml(data.status));
        }
        details.push(updated + ' 更新');
        if (data.stale) {
            details.push('<span style="color:#e05d5d">USB 通信中断，数据已冻结</span>');
        } else if (stale) {
            details.push('<span style="color:#e5a13d">数据已过期</span>');
        }

        return (
            '<div style="display:flex;align-items:stretch;gap:20px">' +
            '<div style="flex:0 0 auto;min-width:164px;display:flex;flex-direction:column;justify-content:center">' +
            '<div style="display:flex;align-items:center;gap:8px;font-size:13px">' +
            '<i class="fa fa-bolt" style="opacity:0.5"></i><b>UPS</b>' +
            '<span style="display:inline-block;padding:1px 8px;border-radius:9px;font-size:11px;font-weight:600;line-height:16px;color:' +
            info.color +
            ';background:' +
            info.bg +
            '">' +
            escapeHtml(info.label) +
            '</span>' +
            '</div>' +
            '<div style="display:flex;align-items:center;gap:9px;margin-top:6px">' +
            batterySvg(charge, levelColor) +
            '<span style="font-size:21px;font-weight:600;line-height:1;color:' +
            levelColor +
            '">' +
            (charge === null ? '—' : Math.round(charge) + '%') +
            '</span>' +
            '</div>' +
            '<div style="font-size:12px;opacity:0.72;margin-top:4px">续航 <b>' +
            runtimeText(data.runtime) +
            '</b></div>' +
            '</div>' +
            '<div style="flex:1 1 auto;min-width:0;display:flex;flex-direction:column;justify-content:center">' +
            '<div style="display:flex;gap:18px">' +
            '<div style="flex:1 1 0;min-width:0">' +
            '<div style="font-size:12px;opacity:0.75;margin-bottom:2px">电池电压 <b>' +
            numberText(data.voltage, 1, ' V') +
            '</b><span style="float:right;opacity:0.5">30 分钟</span></div>' +
            sparkline(history, 1, VOLTAGE_MIN, VOLTAGE_MAX, '#4d9de0', warnVoltage) +
            '</div>' +
            '<div style="flex:1 1 0;min-width:0">' +
            '<div style="font-size:12px;opacity:0.75;margin-bottom:2px">负载 <b>' +
            numberText(data.load, 0, '%') +
            '</b></div>' +
            sparkline(history, 2, 0, loadCeiling(history), '#e5a13d', null) +
            '</div>' +
            '</div>' +
            '<div style="font-size:12px;opacity:0.62;margin-top:3px">' +
            details.join(' · ') +
            '</div>' +
            '</div>' +
            '</div>'
        );
    }

    Ext.define('PVE.node.UpsStatus', {
        extend: 'Ext.Component',
        alias: 'widget.pveUpsStatus',

        initComponent: function () {
            var me = this;

            me.html = render(null);
            me.upsHtml = me.html;
            // Explicit super call: Ext's callParent relies on Function.caller,
            // which is unavailable inside this strict-mode closure.
            Ext.Component.prototype.initComponent.call(me);

            instances.push(me);

            me.on('destroy', function () {
                var index = instances.indexOf(me);
                if (index !== -1) {
                    instances.splice(index, 1);
                }
                if (me.upsTimer) {
                    clearInterval(me.upsTimer);
                }
            });

            me.on('afterrender', me.loadUpsData, me);
            me.upsTimer = setInterval(function () {
                me.loadUpsData();
            }, REFRESH_MS);
        },

        setUpsData: function (data) {
            try {
                var html = render(data);
                if (html === this.upsHtml) {
                    return;
                }
                this.upsHtml = html;
                this.html = html;
                if (this.rendered && this.el && this.el.dom) {
                    this.el.dom.innerHTML = this.html;
                }
            } catch (err) {
                if (window.console) {
                    console.error('UPS status render failed', err);
                }
            }
        },

        loadUpsData: function () {
            var me = this;
            if (me.upsLoading) {
                return;
            }
            me.upsLoading = true;

            var script = document.createElement('script');
            script.async = true;
            script.src = DATA_URL + '?t=' + Date.now();

            var cleanup = function () {
                me.upsLoading = false;
                if (script.parentNode) {
                    script.parentNode.removeChild(script);
                }
            };

            script.onload = cleanup;
            script.onerror = function () {
                cleanup();
                me.setUpsData(null);
            };

            document.head.appendChild(script);
        },
    });

    (function () {
        var proto = PVE.node.StatusView.prototype;
        var originalInitComponent = proto.initComponent;

        if (!originalInitComponent) {
            return;
        }

        proto.initComponent = function () {
            originalInitComponent.apply(this, arguments);

            if (this.upsStatusAdded) {
                return;
            }

            // Never let the extra row break the node summary panel.
            try {
                this.add({
                    xtype: 'pveUpsStatus',
                    itemId: 'upsStatus',
                    colspan: 2,
                    style: {
                        marginTop: '10px',
                        padding: '10px 12px',
                        background: 'rgba(128, 128, 128, 0.06)',
                        borderRadius: '6px',
                    },
                });
                this.upsStatusAdded = true;

                var height = this.height || 360;
                this.setHeight(height + 112);
            } catch (err) {
                if (window.console) {
                    console.error('UPS status row failed', err);
                }
            }
        };
    })();
})();
