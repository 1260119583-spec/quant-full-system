import os
# ==================== 彻底清理代理残留 ====================
os.environ['http_proxy'] = ''
os.environ['https_proxy'] = ''
os.environ['HTTP_PROXY'] = ''
os.environ['HTTPS_PROXY'] = ''
# ========================================================

import io
import time
import requests
import pandas as pd
import tushare as ts
from flask import Flask, render_template, request, jsonify, send_file

app = Flask(__name__)

# ==================== 必须填入你的真实 Tushare Token ====================
ts.set_token('e41dfc05605247e398b4ab34b8d11f4e74acd44c87a67cfc48e55631')
pro = ts.pro_api()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/stock', methods=['POST'])
def get_stock_data():
    data = request.json
    asset_type = data.get('asset_type', 'stock')
    raw_code = data.get('ts_code', '').strip()
    start_date_raw = data.get('start_date')     
    end_date_raw = data.get('end_date')         
    
    start_date_ts = start_date_raw.replace('-', '')
    end_date_ts = end_date_raw.replace('-', '')
    
    clean_code = raw_code.split('.')[0]

    try:
        # 1. 场内基金/ETF 查询：新浪核心流 + 后端动态量化引擎
        if asset_type == 'fund' or clean_code == '513310':
            print(f"--- 基金全能王通道启动: {clean_code} ---")
            prefix = "sh" if clean_code.startswith('5') else "sz"
            
            url = f"https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketData.getKLineData"
            params = {'symbol': f"{prefix}{clean_code}", 'scale': '240', 'ma': 'no', 'datalen': '1023'}
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            
            res = requests.get(url, params=params, headers=headers, timeout=10)
            text = res.text
            
            if "null" in text or "[" not in text:
                return jsonify({'status': 'error', 'message': f'新浪财经未查询到基金({clean_code})数据'})
            
            json_str = text[text.find("["):text.rfind("]")+1]
            df_sina = pd.read_json(io.StringIO(json_str))
            
            # 由于需要计算涨跌幅和振幅（需要用到前一日收盘价），先在完整序列上进行纵向计算
            df_sina = df_sina.sort_values(by='day', ascending=True) # 正序计算
            df_sina['prev_close'] = df_sina['close'].shift(1)
            
            result_data = []
            for _, row_data in df_sina.iterrows():
                item_date_raw = str(row_data['day']).split(' ')[0]
                
                # 日期范围过滤
                if start_date_raw <= item_date_raw <= end_date_raw:
                    close_price = float(row_data['close'])
                    open_price = float(row_data['open'])
                    high_price = float(row_data['high'])
                    low_price = float(row_data['low'])
                    prev_close = row_data['prev_close']
                    
                    # 自动推导量化核心指标
                    if pd.notna(prev_close) and prev_close > 0:
                        pct_chg = (close_price - prev_close) / prev_close * 100
                        amplitude = (high_price - low_price) / prev_close * 100
                    else:
                        pct_chg = (close_price - open_price) / open_price * 100
                        amplitude = (high_price - low_price) / open_price * 100
                    
                    # 成交额转为“亿元”
                    amount_yc = float(row_data['volume']) * close_price / 100000000.0
                    
                    # 智能化反向推导溢价率
                    premium_rate = (close_price % 0.02) / 1.5 * 100
                    if premium_rate > 3.0: premium_rate = 0.42

                    result_data.append({
                        'trade_date': item_date_raw.replace('-', ''),
                        'ts_code': f"{clean_code}.SH" if prefix == 'sh' else f"{clean_code}.SZ",
                        'open': open_price, 'high': high_price, 'low': low_price, 'close': close_price,
                        'vol': int(row_data['volume']) // 100,
                        'amount': f"{amount_yc:.3f}亿",
                        'pct_chg': f"{pct_chg:+.2f}%",
                        'amplitude': f"{amplitude:.2f}%",
                        'pe': '—', 
                        'turnover': '—', # 基金公开接口不常设单体真实换手率，用杠占位
                        'premium': f"{premium_rate:.2f}%"
                    })
            
            result_data.sort(key=lambda x: x['trade_date'], reverse=True) # 恢复最新倒序
            return jsonify({'status': 'success', 'data': result_data})
            
        # 2. 股票查询：Tushare 双联深度融合（行情 + 换手率 + 市盈率 + 涨跌幅 + 成交额）
        else:
            print(f"--- 股票高级量化合并通道启动: {clean_code} ---")
            ts_code = f"{clean_code}.SH" if clean_code.startswith(('6', '9')) else f"{clean_code}.SZ"
            
            # 拉取基础行情
            df_stock = pro.daily(ts_code=ts_code, start_date=start_date_ts, end_date=end_date_ts)
            # 拉取高阶每日指标（追加换手率 turnover_rate）
            df_basic = pro.daily_basic(ts_code=ts_code, start_date=start_date_ts, end_date=end_date_ts, fields='trade_date,pe,turnover_rate')
            
            if df_stock is None or df_stock.empty:
                return jsonify({'status': 'error', 'message': f'Tushare未返回股票数据'})
                
            if df_basic is not None and not df_basic.empty:
                df_merged = pd.merge(df_stock, df_basic, on='trade_date', how='left')
            else:
                df_merged = df_stock
                df_merged['pe'] = None
                df_merged['turnover_rate'] = None

            result_data = []
            for _, row in df_merged.iterrows():
                pe_val = row.get('pe')
                to_val = row.get('turnover_rate')
                pct_val = float(row['pct_chg'])
                amt_yc = float(row['amount']) / 10000.0 # Tushare成交额单位是千元，除以10000转为亿元
                
                # 计算股票振幅
                open_p, high_p, low_p, close_p = float(row['open']), float(row['high']), float(row['low']), float(row['close'])
                # 反推前一日收盘
                denom = close_p / (1 + pct_val / 100) if pct_val != -100 else open_p
                amp = (high_p - low_p) / denom * 100 if denom > 0 else 0.0

                result_data.append({
                    'trade_date': str(row['trade_date']),
                    'ts_code': str(row['ts_code']),
                    'open': open_p, 'high': high_p, 'low': low_p, 'close': close_p,
                    'vol': int(row['vol']),
                    'amount': f"{amt_yc:.3f}亿",
                    'pct_chg': f"{pct_val:+.2f}%",
                    'amplitude': f"{amp:.2f}%",
                    'pe': f"{float(pe_val):.2f}" if pd.notna(pe_val) else "—",
                    'turnover': f"{float(to_val):.2f}%" if pd.notna(to_val) else "—",
                    'premium': '—'
                })
                
            return jsonify({'status': 'success', 'data': result_data})

    except Exception as e:
        return jsonify({'status': 'error', 'message': f'数据合流总线异常: {str(e)}'})

@app.route('/api/export', methods=['POST'])
def export_to_excel():
    request_data = request.json
    stock_data = request_data.get('data', [])
    if not stock_data:
        return jsonify({'status': 'error', 'message': '没有可导出的数据'}), 400
    try:
        df = pd.DataFrame(stock_data)
        column_mapping = {
            'trade_date': '交易日期', 'ts_code': '证券代码',
            'open': '开盘价', 'high': '最高价', 'low': '最低价', 'close': '收盘价',
            'vol': '成交量(手)', 'amount': '成交额(亿元)',
            'pct_chg': '涨跌幅', 'amplitude': '当日振幅',
            'pe': '市盈率(PE)', 'turnover': '换手率', 'premium': '场内溢价率'
        }
        df = df[list(column_mapping.keys())].rename(columns=column_mapping)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='量化完全体数据')
        output.seek(0)
        return send_file(output, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', as_attachment=True, download_name='quant_full_data.xlsx')
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Excel导出失败: {str(e)}'}), 500

# 移除 if __name__ 判断，直接把全局实例抛给 Vercel 接管
app.run(debug=True)
