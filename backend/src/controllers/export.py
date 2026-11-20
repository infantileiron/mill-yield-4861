import csv
import io

from fastapi import HTTPException
from fastapi.responses import Response


def export_review(obj):
    if obj.status not in ('completed', 'approved'):
        raise HTTPException(409, '计算完成后才可导出')
    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(['复核单', obj.id, '标题', obj.title, '状态', obj.status, '范围', obj.line])
    if obj.source == 'indexed':
        w.writerow(['数据来源', '滚动出粉率索引', '索引代', obj.index_generation_id,
                    '水位', obj.index_watermark, '窗口小时', obj.window_hours,
                    '滞后受理', '是' if obj.index_lagging else '否'])
        w.writerow(['窗口起', obj.window_from, '窗口止', obj.window_to,
                    '旧记录（时间未知，未纳入窗口）', obj.legacy_count])
    for k in ['n', 'mean', 'std', 'q1', 'median', 'q3', 'iqr', 'lower', 'upper', 'checksum']:
        if k in obj.result:
            w.writerow([k, obj.result[k]])
    w.writerow(['签核人', obj.approved_by, '签核意见', obj.note])
    w.writerow(['样本编号', '批次', '产线', '采样时刻', '出粉率', '水分', '迟到', '异常'])
    out = {r['sample_code'] for r in obj.result.get('outliers') or []}
    for row in obj.snapshot:
        w.writerow([
            row['sample_code'], row['batch_code'], row['line'], row.get('sampled_at') or '未知',
            row['flour_yield'], row['moisture'], '是' if row.get('late') else '否',
            '是' if row['sample_code'] in out else '否',
        ])
    return Response('﻿' + output.getvalue(), media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="mill-yield-review-{obj.id}.csv"'})
