"""Execute fixed helpers against owned local DOM fixtures; no browser or input."""
import json
import shutil
import subprocess

import pytest

from web_tools.xiaoruo_browser_adapter import _cef_background_script, _cef_helper_script


@pytest.fixture(scope="module")
def node():
    executable = shutil.which("node")
    if executable is None:
        pytest.skip("existing node runtime unavailable")
    return executable


_DOM = r'''
const vm=require("node:vm");
let focusCalls=0,htmlWrites=0,events=[];
class HTMLElement {
 constructor(){this.localName="div";this.type="";this.id="editor";this.nodeType=1;
  this.disabled=false;this.readOnly=false;this.hidden=false;this.isContentEditable=true;
  this.attrs={contenteditable:"true",class:"ql-editor"};this.parentElement=null;
  this._text="old";this._renderedText=undefined;this.nestedControl=false;this.labels=[];}
 focus(){focusCalls++;}
 get textContent(){return this._text;}
 set textContent(text){this._text=String(text);this._renderedText=undefined;}
 get innerText(){return this._renderedText===undefined?this._text:this._renderedText;}
 set innerHTML(_){htmlWrites++;throw new Error("HTML assignment is forbidden");}
 getAttribute(name){return Object.hasOwn(this.attrs,name)?this.attrs[name]:null;}
 hasAttribute(name){return Object.hasOwn(this.attrs,name);}
 getBoundingClientRect(){return {width:this.hidden?0:100,height:20};}
 getRootNode(){return document;}
 closest(){return null;}
 matches(){return false;}
 querySelector(selector){return this.nestedControl?{localName:"input"}:null;}
 dispatchEvent(event){events.push({type:event.type,isTrusted:event.isTrusted});
  if(this.handler)this.handler(event);return true;}
}
class SVGElement extends HTMLElement {}
class HTMLInputElement extends HTMLElement {get value(){return this._value||"";}set value(value){this._value=value;}}
class HTMLTextAreaElement extends HTMLInputElement {}
class HTMLSelectElement extends HTMLInputElement {}
class HTMLFormElement extends HTMLElement {submit(){} requestSubmit(){}}
const el=new HTMLElement();
const window={focus(){focusCalls++;},open(){focusCalls++;}};
const location={href:"https://owned.example.test/editor"};
const CSS={escape:value=>value};
function getComputedStyle(){return {display:"block",visibility:"visible",backgroundImage:"none",cursor:"auto"};}
const document={title:"Owned editor fixture",body:{innerText:"Owned editor fixture"},
 querySelector:()=>null,
 querySelectorAll(selector){
  if(selector==="*"||selector==="#editor")return [el];
  if(selector==="div,span")return el.localName==="div"?[el]:[];
  if(selector==='input[type="file"]')return el.localName==="input"&&el.type==="file"?[el]:[];
  if(selector==="img")return [];
  if(selector.includes('a[href],button,'))return [el];
  return [];
 }};
'''


def _run(node, *, text="new text", setup="", observe=False):
    script = _cef_background_script("type_text", {"selector": "#editor", "text": text})
    observation = _cef_helper_script("read_dom", {"selector": "#editor", "max_chars": 1000})
    # vm executes the exact generated helper. Caller text only occurs in its
    # JSON arguments, and the fixture has no browser, native IPC or system input.
    harness = _DOM + setup + "\nconst script=" + json.dumps(script) + ";\n"
    harness += "const value=JSON.parse(vm.runInThisContext(script));\n"
    if observe:
        harness += "Promise.resolve().then(()=>{const dom=JSON.parse(vm.runInThisContext(" + json.dumps(observation) + "));"
        harness += "process.stdout.write(JSON.stringify({value,dom,events,focusCalls,htmlWrites,text:el.textContent}));});"
    else:
        harness += "process.stdout.write(JSON.stringify({value,events,focusCalls,htmlWrites,text:el.textContent}));"
    result = subprocess.run([node, "-e", harness], capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("mode", ["true", "plaintext-only"])
def test_explicit_editable_replaces_literal_text_without_html_or_focus(node, mode):
    text = '  简介 <img src=x onerror="globalThis.unexpected=true">\n";throw new Error("text");//\n尾行  '
    result = _run(node, text=text, setup=f"el.attrs.contenteditable={json.dumps(mode)};")
    assert result["value"]["verified"] and result["value"]["value_matches"]
    assert result["value"]["observed_value"] == result["text"] == text
    assert result["events"] == [{"type": "input", "isTrusted": False}, {"type": "change", "isTrusted": False}]
    assert result["focusCalls"] == result["htmlWrites"] == 0


@pytest.mark.parametrize("text", ["", "a\n\nb\n", "\r\na\rb\r\n", " leading and trailing \t "])
def test_editable_matches_actual_text_with_only_line_endings_normalized(node, text):
    result = _run(node, text=text)
    expected = text.replace("\r\n", "\n").replace("\r", "\n")
    assert result["text"] == text  # the setter never trims or rewrites caller text
    assert result["value"]["observed_value"] == expected
    assert result["value"]["value_matches"] and result["value"]["verified"]


@pytest.mark.parametrize("setup", [
    'el.attrs.contenteditable="false";',
    'delete el.attrs.contenteditable;',  # inherited editability is not authorization for replacement
    'el.attrs.contenteditable="";',
    'el.isContentEditable=false;',
    'el.nestedControl=true;',
    'el.disabled=true;',
    'el.readOnly=true;',
    'el.hidden=true;',
    'el.attrs["aria-disabled"]="true";',
    'el.attrs["aria-readonly"]="true";',
    'el.type="password";',
    'el.type="file";',
    'el.type="hidden";',
])
def test_unsafe_or_implicit_editable_target_is_refused_before_mutation(node, setup):
    result = _run(node, setup=setup)
    assert not result["value"]["verified"] and not result["value"]["action_may_have_run"]
    assert result["text"] == "old" and not result["events"]
    assert result["focusCalls"] == result["htmlWrites"] == 0


def test_page_transformation_cannot_verify_requested_text_from_its_length(node):
    result = _run(node, text="keep spaces ", setup='el.handler=event=>{if(event.type==="input")el._renderedText="keep spaces";};')
    assert result["value"]["observed_value"] == "keep spaces"
    assert result["value"]["action_may_have_run"] and not result["value"]["value_matches"]
    assert not result["value"]["verified"] and result["focusCalls"] == 0


def test_dynamic_focus_request_is_blocked_and_never_retried(node):
    result = _run(node, setup='el.handler=()=>el.focus();')
    assert result["value"]["error"]["code"] == "foreground_required"
    assert result["value"]["action_may_have_run"] and not result["value"]["verified"]
    assert result["focusCalls"] == 0 and len(result["events"]) == 1


def test_post_mutation_observation_reads_rendered_editable_text_not_concatenated_nodes(node):
    text = " line one\n\nline two \n"
    # A local microtask models an editor observer that reorganizes DOM text into
    # paragraphs. Read DOM must expose actual rendered plain text after it runs.
    setup = 'el.handler=event=>{if(event.type==="input")queueMicrotask(()=>{el._text=" line oneline two ";el._renderedText=' + json.dumps(text) + ';});};'
    result = _run(node, text=text, setup=setup, observe=True)
    selected = result["dom"]["selected_elements"][0]
    assert selected["contenteditable"] == "true" and selected["editable_text"] == text
    assert result["text"] != selected["editable_text"]
    assert result["focusCalls"] == result["htmlWrites"] == 0


def test_post_mutation_observation_reports_editor_rejection_exactly(node):
    result = _run(node, text="requested\ntext", setup='el.handler=event=>{if(event.type==="input")queueMicrotask(()=>{el._renderedText="editor rejected text";});};', observe=True)
    assert result["value"]["observed_value"] == "requested\ntext"
    assert result["dom"]["selected_elements"][0]["editable_text"] == "editor rejected text"


def test_editable_observation_is_bounded_and_preserves_spaces(node):
    text = "  " + "x" * 5000 + "\r\n"
    result = _run(node, text=text, setup='el.attrs.contenteditable="plaintext-only";', observe=True)
    selected = result["dom"]["selected_elements"][0]
    assert selected["contenteditable"] == "plaintext-only"
    assert selected["editable_text"] == text[:4000] and len(selected["editable_text"]) == 4000


@pytest.mark.parametrize("kind", ["password", "hidden", "ordinary", "not_editable", "invisible"])
def test_new_editable_fields_do_not_expose_other_or_hidden_controls(node, kind):
    setup = {
        "password": 'el.localName="input";el.type="password";el._value="secret";',
        "hidden": 'el.localName="input";el.type="hidden";el._value="secret";',
        "ordinary": 'delete el.attrs.contenteditable;el.isContentEditable=false;',
        "not_editable": 'el.attrs.contenteditable="false";el.isContentEditable=false;',
        "invisible": 'el.hidden=true;',
    }[kind]
    result = _run(node, setup=setup, observe=True)
    selected = result["dom"]["selected_elements"][0]
    assert "editable_text" not in selected and "contenteditable" not in selected
    if kind in {"password", "hidden"}:
        assert "value" not in selected


def test_generated_editable_helper_has_no_arbitrary_html_or_foreground_fallback():
    script = _cef_background_script("type_text", {"selector": "#editor", "text": "<script>alert(1)</script>"})
    for forbidden in (".innerHTML=", ".outerHTML=", "execCommand(", ".focus(", "SendInput", "SetForegroundWindow", "system_input"):
        assert forbidden not in script
    assert "el.textContent=expected" in script
