"""Decode the audited public page's embedded disclaimer, without executing JS."""
import hashlib
import json
import re

from bs4 import BeautifulSoup

KPASS_URL='https://www.tossbank.com/card/k-pass'
KPASS_TEXT_PARSER='toss_kpass_hydration_v1'
SECTIONS=['상품 안내','K-패스 안내','온라인 결제 안내','기타 안내']
TEXT_TYPES={'toggle','heading_3','bulleted_list_item','paragraph'}


def parse_kpass_document(content):
    if not content or len(content)>5_000_000:
        raise ValueError('kpass_document_size_invalid')
    scripts=BeautifulSoup(content,'html.parser').find_all('script',id='__NEXT_DATA__',type='application/json')
    if len(scripts)!=1:
        raise ValueError('kpass_hydration_missing_or_ambiguous')
    try:
        data=json.loads(scripts[0].get_text())
        if data['page']!='/card/k-pass': raise ValueError('kpass_page_mismatch')
        queries=data['props']['pageProps']['dehydratedState']['queries']
        resources=[]
        for i,query in enumerate(queries):
            key=query.get('queryKey')
            if isinstance(key,list) and len(key)==2 and key[0]=='TCSDisclaimerResource':
                resources.append((i,key,query['state']['data']))
        if len(resources)!=1: raise ValueError('kpass_disclaimer_ambiguous')
        index,key,resource=resources[0]
        if (resource['isPublished'] is not True or type(resource['postId']) is not int
                or resource['postId']!=key[1] or 'K-패스' not in resource['title']):
            raise ValueError('kpass_disclaimer_identity_invalid')
        blocks=resource['data']
        prefix=f'/props/pageProps/dehydratedState/queries/{index}/state/data/data'
    except (KeyError,TypeError,AttributeError,json.JSONDecodeError,RecursionError):
        raise ValueError('kpass_hydration_structure_changed') from None
    seen=set()
    segments=[]
    references=[]
    text=''

    def rich(parts,path,block):
        nonlocal text
        if not isinstance(parts,list) or len(parts)>200: raise ValueError('kpass_rich_text_invalid')
        values=[]
        for i,part in enumerate(parts):
            if (not isinstance(part,dict) or part.get('type')!='text'
                    or not isinstance(part.get('plain_text'),str)
                    or not isinstance(part.get('text'),dict)
                    or part.get('text',{}).get('content')!=part['plain_text']):
                raise ValueError('kpass_rich_text_conflict')
            values.append(part['plain_text'])
        rendered=re.sub(r'\s+',' ',''.join(values)).strip()
        text+=(' ' if text and rendered else '')
        base=len(text)
        text+=rendered
        cursor=0
        for i,original in enumerate(values):
            value=re.sub(r'\s+',' ',original).strip()
            if value:
                start=rendered.find(value,cursor)
                if start<0: raise ValueError('kpass_quote_mapping_failed')
                segments.append({'json_pointer':f'{path}/{i}/plain_text','block_id':block['id'],
                    'block_type':block['type'],'quote':value,'start_offset':base+start,'end_offset':base+start+len(value)})
                part=parts[i]
                link=part['text'].get('link')
                target=link.get('url') if isinstance(link,dict) else None
                if part.get('href')!=target:
                    raise ValueError('kpass_reference_conflict')
                if target:
                    references.append({'label':value,'target':target,
                        'kind':'document_identifier' if re.fullmatch(r'\d{8}',target) else 'unresolved_reference',
                        'json_pointer':f'{path}/{i}/text/link/url','collection_status':'not_collected'})
                cursor=start+len(value)
        if len(text)>500000: raise ValueError('kpass_text_limit')
        return rendered

    def visit(rows,path,depth=0,table_width=None):
        if not isinstance(rows,list) or depth>12: raise ValueError('kpass_children_invalid')
        headings=[]
        for i,block in enumerate(rows):
            if not isinstance(block,dict): raise ValueError('kpass_block_invalid')
            kind=block.get('type')
            block_id=block.get('id')
            if not isinstance(block_id,str) or not block_id or block_id in seen:
                raise ValueError('kpass_block_identity_invalid')
            seen.add(block_id)
            if len(seen)>2000: raise ValueError('kpass_block_limit')
            if block.get('archived') or block.get('in_trash'): raise ValueError('kpass_archived_block')
            node=block.get(kind)
            if not isinstance(node,dict): raise ValueError('kpass_block_structure_changed')
            base=f'{path}/{i}'
            width=None
            if kind in TEXT_TYPES:
                value=rich(node.get('rich_text'),base+'/'+kind+'/rich_text',block)
                if kind=='toggle': headings.append(value)
            elif kind=='table':
                width=node.get('table_width')
                if type(width) is not int or not 0<width<=20: raise ValueError('kpass_table_invalid')
            elif kind=='table_row':
                cells=node.get('cells')
                if not isinstance(cells,list) or len(cells)!=table_width:
                    raise ValueError('kpass_table_row_invalid')
                for cell_index,cell in enumerate(cells):
                    rich(cell,base+f'/table_row/cells/{cell_index}',block)
            else:
                # Images, embeds and new node types cannot be silently omitted.
                raise ValueError('kpass_unsupported_block_type')
            children=block.get('children',[])
            if not isinstance(children,list): raise ValueError('kpass_children_invalid')
            if block.get('has_children') and not children: raise ValueError('kpass_children_missing')
            if children: visit(children,base+'/children',depth+1,table_width=width)
        return headings
    if visit(blocks,prefix)!=SECTIONS: raise ValueError('kpass_sections_changed')
    if len(text)<100: raise ValueError('kpass_text_incomplete')
    return {'parser_version':KPASS_TEXT_PARSER,'text':text,
            'text_sha256':hashlib.sha256(text.encode()).hexdigest(),
            'sections':SECTIONS,'block_count':len(seen),'segments':segments,
            'references':references,'issues':['benefit_rules_not_extracted','not_publicly_approved',
                *(['linked_documents_not_collected'] if references else [])],
            'resource_post_id':resource['postId'],'scope':'embedded_disclaimer_text_not_benefit_approval'}
