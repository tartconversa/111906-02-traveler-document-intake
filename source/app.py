"""家庭资料领取与独立受理台，仅呈现来源与回执。"""
from .common import *
class App(Application):
    def handle(self,h):
        p=h.path_only
        if p=='/': return self.document(h,'家庭资料门户',view({'today':self.data('now')})+''.join('<p><a href="/traveler/'+x['id']+'">'+x['family']+' / '+x['id']+'</a></p>' for x in self.data('travelers'))+'<a href="/intake">材料受理台</a>')
        if p.startswith('/traveler/'):
            x=next(t for t in self.data('travelers') if t['id']==p.split('/')[-1])
            return self.document(h,'旅客用途资料',view(x)+''.join('<p><a href="/download/'+doc+'?traveler='+x['id']+'&purpose='+x['purpose']+'">下载 '+kind+' '+doc+'</a></p>' for kind,doc in x['required'].items()))
        if p.startswith('/download/'):
            if getattr(h,'new_cookie',None): return self.emit(h,{'error':'session_required'},403)
            x=next(t for t in self.data('travelers') if t['id']==h.query['traveler']);doc=self.seed['documents'][p.split('/')[-1]]
            if x['delegate_until']<self.data('now'): return self.emit(h,{'error':'delegation_expired'},403)
            if h.query['purpose']!=doc['purpose']: return self.emit(h,{'error':'purpose_denied'},403)
            if x['id'] not in doc.get('travelers',[doc.get('traveler')]): return self.emit(h,{'error':'traveler_denied'},403)
            return h.send(doc['content'].encode(),'application/octet-stream',headers={'Content-Disposition':'attachment; filename="'+doc['name']+'"'})
        if p=='/intake':
            if h.command=='POST':
                ref=h.body['reference'];meta=json.loads(h.body['metadata']);files={k:{'name':v['filename'],'sha256':digest(v['bytes'])} for k,v in h.files.items()}
                self.record('intake',ref,{'metadata':meta,'files':files})
                if h.body.get('lose_response')=='yes': return h.send(b'','text/plain',503)
            body=form('/intake',[('reference','送审引用','text',''),('metadata','元数据JSON','text',''),('passport','护照原件','file',''),('guardian','监护原件','file',''),('lose_response','丢失确认','text','')],'送审')
            for ref,item in self.remote.get('intake',{}).items(): body+='<a href="/review/'+ref+'">'+item['receipt']+'</a>'+view(item)
            return self.document(h,'受理台收件历史',body)
        if p.startswith('/review/'):
            ref=p.split('/')[-1];item=self.remote['intake'][ref]
            body=view({'receipt':item['receipt'],'submitted':item['payload'],'results':self.data('reviews')})
            if h.command=='POST':
                key=h.body['reference'];self.record('supplements',key,{'receipt':item['receipt'],'kind':h.body['kind'],'document_id':h.body['document_id'],'sha256':digest(h.files['file']['bytes'])})
            return self.document(h,'逐项受理结果',body+view(self.remote.get('supplements',{}))+form(p,[('reference','补件引用','text',''),('kind','要求材料类型','text','passport'),('document_id','来源材料号','text',''),('file','补件原件','file','')],'提交指定补件'))
        return self.emit(h,{'error':'not_found'},404)
if __name__=='__main__': main(App())
