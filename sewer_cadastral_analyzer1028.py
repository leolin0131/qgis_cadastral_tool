import os
import urllib.request
import zipfile
import tempfile
import ssl
import csv
import subprocess
from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (QgsProcessing,
                       QgsProcessingAlgorithm,
                       QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterEnum,
                       QgsProcessingParameterBoolean,
                       QgsProcessingParameterVectorDestination,
                       QgsProcessingParameterFile,
                       QgsProcessingParameterFileDestination,
                       QgsProcessingParameterString,
                       QgsCoordinateReferenceSystem,
                       QgsProcessingException,
                       QgsVectorLayer,
                       QgsField,
                       QgsFeature,
                       QgsGeometry,
                       QgsPointXY,
                       QgsSpatialIndex,
                       QgsSettings,
                       QgsProcessingUtils)
import processing

class UltimatePipelineLandIntersectionV71(QgsProcessingAlgorithm):

    INPUT_PIPE = 'INPUT_PIPE'
    COUNTY_SELECT = 'COUNTY_SELECT'
    INPUT_CADASTRAL = 'INPUT_CADASTRAL'
    INPUT_FILE = 'INPUT_FILE'
    
    INPUT_SINO_PIPE_CSV = 'INPUT_SINO_PIPE_CSV'
    INPUT_SINO_SIZE_CSV = 'INPUT_SINO_SIZE_CSV'
    INPUT_DIAMETER = 'INPUT_DIAMETER'
    INPUT_THICKNESS = 'INPUT_THICKNESS'
    WIDTH_METHOD = 'WIDTH_METHOD'

    OUTPUT = 'OUTPUT'
    OUTPUT_LAND = 'OUTPUT_LAND'
    OUTPUT_EXCEL = 'OUTPUT_EXCEL'

    COUNTIES = [
        "基隆市", "臺北市", "新北市", "桃園市", 
        "新竹市", "新竹縣", "苗栗縣", "臺中市", 
        "彰化縣", "南投縣", "雲林縣", "嘉義市", 
        "嘉義縣", "臺南市", "高雄市", "屏東縣", 
        "宜蘭縣", "花蓮縣", "臺東縣", "澎湖縣", 
        "金門縣", "連江縣"
    ]

    def tr(self, string): return QCoreApplication.translate('Processing', string)
    def createInstance(self): return UltimatePipelineLandIntersectionV71()
    def name(self): return 'ultimate_pipeline_land_intersection_v71'
    def displayName(self): return self.tr('【V71】管線地籍全自動分析工具')
    def group(self): return self.tr('下水道規劃自動化工具')
    def groupId(self): return 'sewerage_planning_tools'

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(self.INPUT_PIPE, self.tr('1. 污水管線圖層 (支援 DWG/DXF/SHP/KML，自動轉 TWD97)'), [QgsProcessing.TypeVectorLine]))
        self.addParameter(QgsProcessingParameterEnum(self.COUNTY_SELECT, self.tr('2. 請選擇分析的「縣市」'), options=self.COUNTIES, defaultValue=3))
        self.addParameter(QgsProcessingParameterFeatureSource(self.INPUT_CADASTRAL, self.tr('3. [選填] 自備全區地籍圖 (有選取則忽略雲端公有地下載)'), [QgsProcessing.TypeVectorPolygon], optional=True))
        self.addParameter(QgsProcessingParameterFile(self.INPUT_FILE, self.tr('4. 全台段號對照表 (請載入 UTF-8 CSV)'), optional=True))
        
        self.addParameter(QgsProcessingParameterFile(self.INPUT_SINO_PIPE_CSV, self.tr('5. [選填] 匯入Sinosewer管線設計檔 (Pipe.csv)'), optional=True))
        self.addParameter(QgsProcessingParameterFile(self.INPUT_SINO_SIZE_CSV, self.tr('6. [選填] 匯入Sinosewer管徑資料檔 (PipeSize.csv)'), optional=True))
        self.addParameter(QgsProcessingParameterString(self.INPUT_DIAMETER, self.tr('7. [預設參數] 統一標稱管徑 (mm) (未匯入設計檔時套用)'), defaultValue='', optional=True))
        self.addParameter(QgsProcessingParameterString(self.INPUT_THICKNESS, self.tr('8. [預設參數] 統一管壁厚度 (mm) (未匯入設計檔時套用)'), defaultValue='', optional=True))
        
        self.addParameter(QgsProcessingParameterEnum(self.WIDTH_METHOD, self.tr('10. 投影寬度計算法規選項'), options=['依寬度 × 1.5 計算 (不足 1 米以 1 米計)', '依原始寬度計算 (不足 1 米以 1 米計)'], defaultValue=0))
        
        self.addParameter(QgsProcessingParameterVectorDestination(self.OUTPUT, self.tr('11. 產出結果_管線段交集報表 (.gpkg)')))
        self.addParameter(QgsProcessingParameterVectorDestination(self.OUTPUT_LAND, self.tr('12. [選填] 匯出受影響之完整地籍圖層 (償金出圖用 .gpkg)'), optional=True))
        self.addParameter(QgsProcessingParameterFileDestination(self.OUTPUT_EXCEL, self.tr('13. [選填] 同步匯出 Excel 報表 (.xlsx)'), 'Excel Files (*.xlsx)', None, True))
        self.addParameter(QgsProcessingParameterBoolean('RUN_CRAWLER', self.tr('9. 執行完畢後，自動啟動雲端產權爬蟲'), defaultValue=False))

    def processAlgorithm(self, parameters, context, feedback):
        QgsSettings().setValue("/Processing/encoding", "UTF-8")
        os.environ["SHAPE_ENCODING"] = "UTF-8"
        os.environ["GDAL_FILENAME_IS_UTF8"] = "YES"
        
        target_crs = QgsCoordinateReferenceSystem("EPSG:3826")
        output_dest = self.parameterAsOutputLayer(parameters, self.OUTPUT, context)
        output_land_dest = self.parameterAsOutputLayer(parameters, self.OUTPUT_LAND, context)
        dest_land = output_land_dest if output_land_dest and output_land_dest != 'NULL' else 'TEMPORARY_OUTPUT'

        selected_county_idx = self.parameterAsEnum(parameters, self.COUNTY_SELECT, context)
        county_name = self.COUNTIES[selected_county_idx]
        width_method_idx = self.parameterAsEnum(parameters, self.WIDTH_METHOD, context)
        
        cadastral_val = self.parameterAsString(parameters, self.INPUT_CADASTRAL, context)
        has_local_cadastral = bool(cadastral_val and cadastral_val.strip() and cadastral_val != 'NULL')

        file_path = self.parameterAsFile(parameters, self.INPUT_FILE, context)
        if file_path: file_path = str(file_path).strip().strip('"').strip("'")
        has_local_file = bool(file_path and os.path.isfile(file_path))
        csv_layer = None

        if has_local_file:
            safe_path = file_path.replace('\\', '/')
            uri_utf8 = f"file:///{safe_path}?encoding=UTF-8&delimiter=,&geomType=none&detectTypes=no"
            csv_layer = QgsVectorLayer(uri_utf8, "csv_layer", "delimitedtext")
            if not csv_layer.isValid() or '縣市名稱' not in [f.name() for f in csv_layer.fields()]:
                uri_big5 = f"file:///{safe_path}?encoding=Big5&delimiter=,&geomType=none&detectTypes=no"
                csv_layer = QgsVectorLayer(uri_big5, "csv_layer", "delimitedtext")

        def read_csv_safe(fp):
            rows = []
            for enc in ['utf-8-sig', 'big5', 'cp950', 'utf-8', 'utf-16', 'utf-16le']:
                try:
                    with open(fp, 'r', encoding=enc) as f:
                        header_line = f.readline()
                        if not header_line: continue
                        f.seek(0)
                        delimiter = '\t' if '\t' in header_line else ','
                        reader = csv.DictReader(f, delimiter=delimiter)
                        for r in reader:
                            cleaned_row = {str(k).lower().strip(): str(v).strip() for k, v in r.items() if k}
                            if any(cleaned_row.values()): rows.append(cleaned_row)
                    if rows: return rows
                except: pass
            return rows

        def get_float_val(row_dict, key_name):
            if key_name in row_dict:
                val = row_dict[key_name]
                try:
                    if val is None or str(val).strip() == '': return 0.0
                    return float(str(val).replace(',', '').strip())
                except: return 0.0
            return 0.0

        csv_pipe_path = self.parameterAsFile(parameters, self.INPUT_SINO_PIPE_CSV, context)
        csv_size_path = self.parameterAsFile(parameters, self.INPUT_SINO_SIZE_CSV, context)
        if csv_pipe_path: csv_pipe_path = str(csv_pipe_path).strip().strip('"').strip("'")
        if csv_size_path: csv_size_path = str(csv_size_path).strip().strip('"').strip("'")
        has_csv = bool(csv_pipe_path and os.path.isfile(csv_pipe_path))

        pipe_rows, size_rows, size_dict = [], [], {}
        if has_csv:
            pipe_rows = read_csv_safe(csv_pipe_path)
            if csv_size_path and os.path.isfile(csv_size_path):
                size_rows = read_csv_safe(csv_size_path)
            if size_rows:
                for r in size_rows:
                    d = get_float_val(r, 'nomd')
                    t = get_float_val(r, 'nomt')
                    if d > 0: size_dict[d] = t

        feedback.pushInfo("\n【效能優化啟動】執行管線投影與空間屬性綁定...")
        reprojected_pipe = processing.run("native:reprojectlayer", {
            'INPUT': parameters[self.INPUT_PIPE], 'TARGET_CRS': target_crs, 'OUTPUT': 'TEMPORARY_OUTPUT'
        }, context=context, is_child_algorithm=True)['OUTPUT']
        
        pipe_fixed = processing.run("native:fixgeometries", {
            'INPUT': reprojected_pipe, 'OUTPUT': 'TEMPORARY_OUTPUT'
        }, context=context, is_child_algorithm=True)['OUTPUT']

        pipe_layer_obj = context.getMapLayer(pipe_fixed)
        existing_fields = [f.name() for f in pipe_layer_obj.fields()]
        
        junk_sys_fields = [
            'description', 'timestamp', 'begin', 'end', 'altitudemode', 'tessellate', 'extrude', 'visibility', 'draworder', 'icon',
            'space', 'layer', 'olinetype', 'linetype', 'color', 'ocolor', 'color24', 'transparency', 'transparen', 'lweight', 'linewidth', 'ltscale', 'thickness', 'ext', 'width', 'handle', 'block', 'etype', 'visible', 'path',
            'id', 'objectid', 'uuid', 'shape_length', 'shape_area', 'fid_2', 'handle_2', 'block_2', 'etype_2', 'space_2', 'layer_2', 'olinetype_', 'linetype_2', 'color_2', 'ocolor_2', 'color24_2', 'transpar_1', 'lweight_2',
            'linewidth_', 'ltscale_2', 'visible_2', 'width_2', 'thickness_', 'ext_2'
        ]
        
        original_pipe_fields = []
        for name in existing_fields:
            lower_name = name.lower()
            if lower_name == 'fid' or name in ['csv_join_code', 'target_join_code', '_join_code', 'sino_nomd', 'sino_nomt', 'sino_pipeno']: continue
            if lower_name in junk_sys_fields: continue
            if name == '長度' or lower_name == 'length': continue
            if len(name) > 10 and name.isupper() and all(c in '0123456789ABCDEF' for c in name): continue
            original_pipe_fields.append(name)
            
        fields_to_drop = [f for f in existing_fields if f not in original_pipe_fields]
        if fields_to_drop:
            pipe_fixed = processing.run("native:deletecolumn", {
                'INPUT': pipe_fixed, 'COLUMN': fields_to_drop, 'OUTPUT': 'TEMPORARY_OUTPUT'
            }, context=context, is_child_algorithm=True)['OUTPUT']

        pipe_fixed = processing.run("native:fieldcalculator", {
            'INPUT': pipe_fixed, 'FIELD_NAME': 'sino_pipeno', 'FIELD_TYPE': 2, 'FIELD_LENGTH': 50, 'FORMULA': "''", 'OUTPUT': 'TEMPORARY_OUTPUT'
        }, context=context, is_child_algorithm=True)['OUTPUT']
        pipe_fixed = processing.run("native:fieldcalculator", {
            'INPUT': pipe_fixed, 'FIELD_NAME': 'sino_nomd', 'FIELD_TYPE': 0, 'FIELD_LENGTH': 10, 'FORMULA': "0", 'OUTPUT': 'TEMPORARY_OUTPUT'
        }, context=context, is_child_algorithm=True)['OUTPUT']
        pipe_fixed = processing.run("native:fieldcalculator", {
            'INPUT': pipe_fixed, 'FIELD_NAME': 'sino_nomt', 'FIELD_TYPE': 0, 'FIELD_LENGTH': 10, 'FORMULA': "0", 'OUTPUT': 'TEMPORARY_OUTPUT'
        }, context=context, is_child_algorithm=True)['OUTPUT']

        sino_keys, sino_data_map = set(), {}
        if pipe_rows:
            for r in pipe_rows:
                p_no = str(r.get('pipeno', '')).strip()
                nomd = get_float_val(r, 'nomd')
                nomt = size_dict.get(nomd, 0.0)
                if p_no: 
                    sino_keys.add(p_no)
                    sino_data_map[p_no] = (p_no, nomd, nomt)
            
        best_field, best_match_count = None, 0
        pipe_layer_obj = context.getMapLayer(pipe_fixed)
        idx_pipeno = pipe_layer_obj.fields().indexOf('sino_pipeno')
        idx_nomd = pipe_layer_obj.fields().indexOf('sino_nomd')
        idx_nomt = pipe_layer_obj.fields().indexOf('sino_nomt')
        
        if pipe_layer_obj and sino_keys:
            for field in pipe_layer_obj.fields():
                match_count = sum(1 for feat in pipe_layer_obj.getFeatures() if str(feat[field.name()]).strip() in sino_keys)
                if match_count > best_match_count:
                    best_match_count = match_count
                    best_field = field.name()

        updates, pipe_pr = {}, pipe_layer_obj.dataProvider()
        if best_match_count > 0:
            for feat in pipe_layer_obj.getFeatures():
                val = str(feat[best_field]).strip()
                if val in sino_data_map:
                    data = sino_data_map[val]
                    updates[feat.id()] = {idx_pipeno: data[0], idx_nomd: data[1], idx_nomt: data[2]}
            if updates: pipe_pr.changeAttributeValues(updates)
        elif pipe_rows:
            mem_layer = QgsVectorLayer("LineString?crs=EPSG:3826", "SinoPipes", "memory")
            pr = mem_layer.dataProvider()
            pr.addAttributes([QgsField("join_pipeno", QVariant.String), QgsField("sino_nomd", QVariant.Double), QgsField("sino_nomt", QVariant.Double)])
            mem_layer.updateFields()
            features = []
            for r in pipe_rows:
                sx, sy = get_float_val(r, 'startx'), get_float_val(r, 'starty')
                ex, ey = get_float_val(r, 'endx'), get_float_val(r, 'endy')
                nomd, pipeno = get_float_val(r, 'nomd'), str(r.get('pipeno', '')).strip()
                if sx > 100000 and sy > 1000000 and ex > 100000 and ey > 1000000 and nomd > 0:
                    feat = QgsFeature(mem_layer.fields())
                    feat.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(sx, sy), QgsPointXY(ex, ey)]))
                    feat.setAttribute('join_pipeno', pipeno)
                    feat.setAttribute('sino_nomd', nomd)
                    feat.setAttribute('sino_nomt', size_dict.get(nomd, 0.0))
                    features.append(feat)
            pr.addFeatures(features)
            
            if features:
                index = QgsSpatialIndex(mem_layer.getFeatures())
                sino_dict = {f.id(): f for f in mem_layer.getFeatures()}
                distances, match_count = [], 0
                for feat in pipe_layer_obj.getFeatures():
                    n_ids = index.nearestNeighbor(feat.geometry(), 1)
                    if n_ids:
                        n_feat = sino_dict[n_ids[0]]
                        dist = feat.geometry().distance(n_feat.geometry())
                        distances.append(dist)
                        if dist <= 100:
                            updates[feat.id()] = {idx_pipeno: n_feat['join_pipeno'], idx_nomd: n_feat['sino_nomd'], idx_nomt: n_feat['sino_nomt']}
                            match_count += 1
                if updates: pipe_pr.changeAttributeValues(updates)

        # ==========================================
        # 核心優化：先處理地籍圖屬性再交集，確保雙邊資料完美對接
        # ==========================================
        feedback.pushInfo("\n▶ 執行空間修補與地籍屬性重構 ...")
        if has_local_cadastral:
            land_reproj = processing.run("native:reprojectlayer", {'INPUT': parameters[self.INPUT_CADASTRAL], 'TARGET_CRS': target_crs, 'OUTPUT': 'TEMPORARY_OUTPUT'}, context=context, is_child_algorithm=True)['OUTPUT']
            land_fixed = processing.run("native:fixgeometries", {'INPUT': land_reproj, 'OUTPUT': 'TEMPORARY_OUTPUT'}, context=context, is_child_algorithm=True)['OUTPUT']
        else:
            code = {"基隆市":"C", "臺北市":"A", "新北市":"F", "桃園市":"H", "新竹市":"O", "新竹縣":"J", "苗栗縣":"K", "臺中市":"B", "彰化縣":"N", "南投縣":"M", "雲林縣":"P", "嘉義市":"I", "嘉義縣":"Q", "臺南市":"D", "高雄市":"E", "屏東縣":"T", "宜蘭縣":"G", "花蓮縣":"U", "臺東縣":"V", "澎湖縣":"X", "金門縣":"W", "連江縣":"Z"}[county_name]
            download_url = f"https://easymap.moi.gov.tw/R01OpenData/Opendata/Download?type=zip&fileName={code}&fileFormat=KML"
            try:
                temp_dir = tempfile.mkdtemp()
                zip_path = os.path.join(temp_dir, "data.zip")
                context_ssl = ssl._create_unverified_context()
                req = urllib.request.Request(download_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, context=context_ssl) as response, open(zip_path, 'wb') as out_file: 
                    out_file.write(response.read())
                with zipfile.ZipFile(zip_path, 'r') as zip_ref: zip_ref.extractall(temp_dir)
            except Exception as e: 
                raise QgsProcessingException(f"雲端下載失敗！原因：{str(e)}")
            kml_files = [os.path.join(r, f) for r, d, files in os.walk(temp_dir) for f in files if f.lower().endswith('.kml')]
            raw_land = processing.run("native:mergevectorlayers", {'LAYERS': kml_files, 'CRS': None, 'OUTPUT': 'TEMPORARY_OUTPUT'}, context=context, is_child_algorithm=True)['OUTPUT']
            land_reproj = processing.run("native:reprojectlayer", {'INPUT': raw_land, 'TARGET_CRS': target_crs, 'OUTPUT': 'TEMPORARY_OUTPUT'}, context=context, is_child_algorithm=True)['OUTPUT']
            land_fixed = processing.run("native:fixgeometries", {'INPUT': land_reproj, 'OUTPUT': 'TEMPORARY_OUTPUT'}, context=context, is_child_algorithm=True)['OUTPUT']

        land_extracted = processing.run("native:extractbylocation", {'INPUT': land_fixed, 'PREDICATE': [0], 'INTERSECT': pipe_fixed, 'OUTPUT': 'TEMPORARY_OUTPUT'}, context=context, is_child_algorithm=True)['OUTPUT']

        poly_join_field = 'AA48' if has_local_cadastral else 'SECTNO'
        land_extracted = processing.run("native:fieldcalculator", {
            'INPUT': land_extracted, 'FIELD_NAME': '_join_code', 'FIELD_TYPE': 2, 'FIELD_LENGTH': 10, 'FORMULA': f"lpad(trim(to_string(\"{poly_join_field}\")), 4, '0')", 'OUTPUT': 'TEMPORARY_OUTPUT'
        }, context=context, is_child_algorithm=True)['OUTPUT']

        if has_local_file and csv_layer:
            county_name_alt = county_name.replace('臺', '台')
            expression = f"\"縣市名稱\" LIKE '%{county_name}%' OR \"縣市名稱\" LIKE '%{county_name_alt}%'"
            filtered_csv = processing.run("native:extractbyexpression", {
                'INPUT': csv_layer, 'EXPRESSION': expression, 'OUTPUT': 'TEMPORARY_OUTPUT'
            }, context=context, is_child_algorithm=True)['OUTPUT']
            filtered_csv = processing.run("native:fieldcalculator", {
                'INPUT': filtered_csv, 'FIELD_NAME': 'csv_join_code', 'FIELD_TYPE': 2, 'FIELD_LENGTH': 10, 'FORMULA': "lpad(trim(to_string(\"代碼\")), 4, '0')", 'OUTPUT': 'TEMPORARY_OUTPUT'
            }, context=context, is_child_algorithm=True)['OUTPUT']
            land_extracted = processing.run("native:joinattributestable", {
                'INPUT': land_extracted, 'FIELD': '_join_code', 'INPUT_2': filtered_csv, 'FIELD_2': 'csv_join_code', 'FIELDS_TO_COPY': [], 'METHOD': 1, 'DISCARD_NONMATCHING': False, 'PREFIX': 'csv_', 'OUTPUT': 'TEMPORARY_OUTPUT'
            }, context=context, is_child_algorithm=True)['OUTPUT']

        # 為地籍圖多邊形進行大掃除，去除 KML 垃圾並建立乾淨中文欄位
        layer_land_temp = context.getMapLayer(land_extracted)
        land_temp_fields = [f.name() for f in layer_land_temp.fields()]
        
        town_candidates = []
        for exact in ['csv_鄉鎮市區', 'csv_鄉鎮區', 'csv_鄉鎮市區名稱', 'csv_行政區', 'csv_行政區域名稱', 'csv_區', 'csv_鄉鎮', 'csv_鄉鎮名稱', 'csv_市區']:
            if exact in land_temp_fields: town_candidates.append(exact)
        for f in land_temp_fields:
            if f.startswith('csv_') and ('鄉鎮' in f or '市區' in f or '行政' in f) and f not in town_candidates:
                town_candidates.append(f)
        town_candidates.extend([f for f in ['TOWNNAME', 'TOWN'] if f in land_temp_fields])
        
        if town_candidates:
            args_list = []
            for f in town_candidates:
                args_list.append('NULLIF(trim(to_string("{}")), \'\')'.format(f))
            town_expr = "COALESCE({}, '未知')".format(", ".join(args_list))
        else:
            town_expr = "'未知'"

        if has_local_file and csv_layer:
            prefix_str = '' if has_local_cadastral else '公有地'
            segment_sql = f"CASE WHEN \"csv_段\" IS NULL THEN '{prefix_str}未知(代碼:' || \"_join_code\" || ')' ELSE \"csv_段\" || CASE WHEN \"csv_小段\" IS NULL OR trim(\"csv_小段\") = '' THEN '段' ELSE '段' || \"csv_小段\" || '小段' END END"
        else:
            segment_sql = "'未載入對照表'" if has_local_cadastral else "'公有地(無載入對照表)'"

        if has_local_cadastral:
            parcel_sql = "CASE WHEN \"PMNO\" IS NULL THEN '無' ELSE to_string(to_int(\"PMNO\")) || CASE WHEN to_int(\"PCNO\") > 0 THEN '-' || to_string(to_int(\"PCNO\")) ELSE '' END END"
        else:
            parcel_sql = "CASE WHEN \"PARCELNO\" IS NULL THEN '無' ELSE \"PARCELNO\" END"

        land_mapping = []
        land_mapping.append({'name': '行政區', 'type': QVariant.String, 'length': 20, 'precision': 0, 'expression': town_expr})
        land_mapping.append({'name': '地段代碼', 'type': QVariant.String, 'length': 10, 'precision': 0, 'expression': '"_join_code"'})
        land_mapping.append({'name': '地段', 'type': QVariant.String, 'length': 50, 'precision': 0, 'expression': segment_sql})
        land_mapping.append({'name': '地號', 'type': QVariant.String, 'length': 50, 'precision': 0, 'expression': parcel_sql})
        
        # 隱藏與剃除所有 KML 的無用垃圾屬性
        junk_poly_fields = ['name', 'description', 'timestamp', 'begin', 'end', 'altitudemode', 'tessellate', 'extrude', 'visibility', 'draworder', 'icon', 'layer', 'path', '_join_code']
        for f in layer_land_temp.fields():
            fn = f.name()
            fn_lower = fn.lower()
            if fn_lower not in junk_poly_fields and not fn_lower.startswith('csv_'):
                land_mapping.append({'name': fn, 'type': f.type(), 'length': f.length(), 'precision': f.precision(), 'expression': f'"{fn}"'})

        # 將乾淨的地籍圖匯出至 dest_land (這份就是要給您的完美出圖資料)
        land_clean = processing.run("native:refactorfields", {
            'INPUT': land_extracted, 'FIELDS_MAPPING': land_mapping, 'OUTPUT': dest_land
        }, context=context, is_child_algorithm=True)['OUTPUT']

        # ==========================================
        # 管線交集報表產出
        # ==========================================
        feedback.pushInfo("\n▶ 執行終端交集與管線報表生成...")
        temp_intersected = processing.run("native:intersection", {
            'INPUT': pipe_fixed, 'OVERLAY': land_clean, 'INPUT_FIELDS': [], 'OVERLAY_FIELDS': [], 'OVERLAY_FIELDS_PREFIX': '', 'OUTPUT': 'TEMPORARY_OUTPUT'
        }, context=context, feedback=feedback, is_child_algorithm=True)['OUTPUT']

        d_str = self.parameterAsString(parameters, self.INPUT_DIAMETER, context)
        t_str = self.parameterAsString(parameters, self.INPUT_THICKNESS, context)
        d_mm_g = float(d_str) if d_str.strip() else 0.0
        t_mm_g = float(t_str) if t_str.strip() else 0.0

        if width_method_idx == 0:
            sql_w_eff = f"CASE WHEN COALESCE(\"sino_nomd\", 0) > 0 THEN max(((COALESCE(\"sino_nomd\", 0) + 2 * COALESCE(\"sino_nomt\", 0)) / 1000.0) * 1.5, 1.0) WHEN {d_mm_g} > 0 THEN max((({d_mm_g} + 2 * {t_mm_g}) / 1000.0) * 1.5, 1.0) ELSE 0 END"
        else:
            sql_w_eff = f"CASE WHEN COALESCE(\"sino_nomd\", 0) > 0 THEN max(((COALESCE(\"sino_nomd\", 0) + 2 * COALESCE(\"sino_nomt\", 0)) / 1000.0), 1.0) WHEN {d_mm_g} > 0 THEN max((({d_mm_g} + 2 * {t_mm_g}) / 1000.0), 1.0) ELSE 0 END"

        layer_temp = context.getMapLayer(temp_intersected)
        mapping = []
        mapping.append({'name': '縣市', 'type': QVariant.String, 'length': 10, 'precision': 0, 'expression': f"'{county_name}'"})
        
        mapping.append({'name': '管線編號', 'type': QVariant.String, 'length': 50, 'precision': 0, 'expression': "CASE WHEN \"sino_pipeno\" = '' OR \"sino_pipeno\" IS NULL THEN '未知' ELSE \"sino_pipeno\" END"})
        
        for f_name in original_pipe_fields:
            idx = layer_temp.fields().indexOf(f_name)
            if idx != -1:
                f = layer_temp.fields().at(idx)
                mapping.append({'name': f.name(), 'type': f.type(), 'length': f.length(), 'precision': f.precision(), 'expression': f'"{f.name()}"'})
                
        mapping.extend([
            {'name': '行政區', 'type': QVariant.String, 'length': 20, 'precision': 0, 'expression': '"行政區"'},
            {'name': '地段代碼', 'type': QVariant.String, 'length': 10, 'precision': 0, 'expression': '"地段代碼"'},
            {'name': '地段', 'type': QVariant.String, 'length': 50, 'precision': 0, 'expression': '"地段"'},
            {'name': '地號', 'type': QVariant.String, 'length': 50, 'precision': 0, 'expression': '"地號"'},
            {'name': '管線經過長度(m)', 'type': QVariant.Double, 'length': 15, 'precision': 2, 'expression': "round($length, 2)"},
            {'name': '標稱管徑(mm)', 'type': QVariant.Double, 'length': 10, 'precision': 0, 'expression': f"CASE WHEN \"sino_nomd\" > 0 THEN \"sino_nomd\" ELSE {d_mm_g} END"},
            {'name': '管厚(mm)', 'type': QVariant.Double, 'length': 10, 'precision': 0, 'expression': f"CASE WHEN \"sino_nomt\" > 0 THEN \"sino_nomt\" ELSE {t_mm_g} END"}
        ])

        if pipe_rows or d_mm_g > 0:
            mapping.append({'name': '償金計算面積(m2)', 'type': QVariant.Double, 'length': 15, 'precision': 2, 'expression': f"round($length * ({sql_w_eff}), 2)"})

        final_result = processing.run("native:refactorfields", {
            'INPUT': temp_intersected, 'FIELDS_MAPPING': mapping, 'OUTPUT': output_dest
        }, context=context, is_child_algorithm=True)['OUTPUT']

        pipe_layer_obj = QgsProcessingUtils.mapLayerFromString(pipe_fixed, context)
        intersected_layer_obj = QgsProcessingUtils.mapLayerFromString(temp_intersected, context)
        
        if pipe_layer_obj and intersected_layer_obj:
            total_original_length = sum([f.geometry().length() for f in pipe_layer_obj.getFeatures() if f.hasGeometry()])
            total_intersect_length = sum([f.geometry().length() for f in intersected_layer_obj.getFeatures() if f.hasGeometry()])
            if total_original_length > 0:
                feedback.pushInfo("\n" + "★" * 35)
                feedback.pushInfo(f"【 佔 用 統 計 報 告 】")
                feedback.pushInfo(f"▶ 管線設計總長度 : {total_original_length:,.2f} 公尺")
                if has_local_cadastral:
                    feedback.pushInfo(f"▶ 位於指定地籍圖長度 : {total_intersect_length:,.2f} 公尺")
                    feedback.pushInfo(f"▶ 已登錄地籍涵蓋率 : {(total_intersect_length / total_original_length) * 100:.2f} %")
                else:
                    feedback.pushInfo(f"▶ 位於公有地長度 : {total_intersect_length:,.2f} 公尺")
                    feedback.pushInfo(f"▶ 公有地佔用比例 : {(total_intersect_length / total_original_length) * 100:.2f} %")
                feedback.pushInfo("★" * 35 + "\n")

                raw_excel_val = parameters.get(self.OUTPUT_EXCEL)
        if raw_excel_val and str(raw_excel_val).strip() and str(raw_excel_val) not in ['TEMPORARY_OUTPUT', 'NULL']:
            excel_path = self.parameterAsFileOutput(parameters, self.OUTPUT_EXCEL, context)
        if excel_path:
                try: 
                    processing.run("native:savefeatures", {'INPUT': final_result, 'OUTPUT': excel_path}, context=context, is_child_algorithm=True)
                    
                    # 讀取選項 13 是否有被打勾
                    run_crawler = self.parameterAsBool(parameters, 'RUN_CRAWLER', context)
                    
                    # 只有當 13 有打勾，才執行爬蟲
                    if run_crawler:
                        # 注意：這裡必須指到具體的 .exe 執行檔，不能只有 dist 資料夾
                        crawler_exe_path = r"C:\Users\7762\nlsc_crawler\dist\nlsc_crawler.exe" 
                        
                        if os.path.exists(crawler_exe_path):
                            feedback.pushInfo("\n▶ 選項13已勾選：正在自動啟動爬蟲程式...")
                            subprocess.Popen([crawler_exe_path, excel_path])
                        else:
                            feedback.pushInfo("\n⚠️ 找不到爬蟲執行檔，請確認路徑是否包含 .exe 檔名。")
                            
                except Exception as e: 
                    feedback.pushInfo(f"產出報表或呼叫爬蟲時發生錯誤: {e}")
                    
        feedback.pushInfo("\n【系統訊息】 V71 完美出圖版分析完成！")
        
        results = {self.OUTPUT: final_result}
        if dest_land != 'TEMPORARY_OUTPUT':
            results[self.OUTPUT_LAND] = dest_land
            
        return results