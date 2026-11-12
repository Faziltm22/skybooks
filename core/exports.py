from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile
from xml.sax.saxutils import escape


def _column_name(number):
    result=''
    while number:
        number,remainder=divmod(number-1,26)
        result=chr(65+remainder)+result
    return result


def make_xlsx(sheets):
    """Create a minimal, dependency-free XLSX workbook from (title, rows) pairs."""
    buffer=BytesIO()
    with ZipFile(buffer,'w',ZIP_DEFLATED) as archive:
        sheet_xml=[]
        for index,(title,rows) in enumerate(sheets,1):
            body=[]
            for row_index,row in enumerate(rows,1):
                cells=[]
                for column_index,value in enumerate(row,1):
                    if value is None: continue
                    ref=f'{_column_name(column_index)}{row_index}'
                    if isinstance(value,(bool,int,float,Decimal)) and not isinstance(value,bool):
                        cell=f'<c r="{ref}"><v>{escape(str(value))}</v></c>'
                    elif isinstance(value,bool):
                        cell=f'<c r="{ref}" t="b"><v>{1 if value else 0}</v></c>'
                    else:
                        if isinstance(value,(date,datetime)): value=value.isoformat()
                        text=escape(str(value))
                        cell=f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'
                    cells.append(cell)
                body.append(f'<row r="{row_index}">{"".join(cells)}</row>')
            sheet_xml.append((title[:31] or f'Sheet{index}',f'xl/worksheets/sheet{index}.xml'))
            archive.writestr(f'xl/worksheets/sheet{index}.xml','<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'+''.join(body)+'</sheetData></worksheet>')
        content_types=['<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>','<Default Extension="xml" ContentType="application/xml"/>','<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>']
        content_types.extend(f'<Override PartName="/{path}" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for _,path in sheet_xml)
        archive.writestr('[Content_Types].xml','<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'+''.join(content_types)+'</Types>')
        archive.writestr('_rels/.rels','<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        sheet_nodes=[]; relationship_nodes=[]
        for index,(title,path) in enumerate(sheet_xml,1):
            sheet_nodes.append(f'<sheet name="{escape(title)}" sheetId="{index}" r:id="rId{index}"/>')
            relationship_nodes.append(f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>')
        archive.writestr('xl/workbook.xml','<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'+''.join(sheet_nodes)+'</sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels','<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+''.join(relationship_nodes)+'</Relationships>')
    return buffer.getvalue()
