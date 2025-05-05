#!/usr/bin/env python3
"""
Codex CLI: generates Spring Boot boilerplate with Swagger specs.
"""

import os
import json
import io
import zipfile
import shutil
import subprocess
import requests
import sys
import re

from rich import print
from rich.prompt import Prompt, Confirm
from rich.console import Console
from rich.table import Table

def fetch_metadata():
    # Fetch Initializr metadata (build types, versions, dependencies) from the client endpoint
    resp = requests.get("https://start.spring.io/metadata/client")
    resp.raise_for_status()
    return resp.json()

def select_option(prompt_text, options):
    console = Console()
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Index", style="dim", width=6)
    table.add_column("ID")
    table.add_column("Name")
    for idx, opt in enumerate(options):
        table.add_row(str(idx), opt['id'], opt.get('name', ''))
    console.print(table)
    choice = Prompt.ask(prompt_text, default="0")
    try:
        idx = int(choice)
        if 0 <= idx < len(options):
            return options[idx]['id']
    except:
        pass
    print("[red]Invalid selection, try again.[/red]")
    return select_option(prompt_text, options)

def fuzzy_select_dependencies(deps):
    console = Console()
    pool = deps.copy()
    selected = []
    while True:
        term = Prompt.ask("Dependency search (blank to finish)", default="")
        if not term:
            break
        matches = [d for d in pool if term.lower() in d['id'].lower() or term.lower() in d.get('name','').lower() or term.lower() in d.get('description','').lower()]
        if not matches:
            print(f"[yellow]No matches for '{term}'[/yellow]")
            continue
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Index", style="dim", width=6)
        table.add_column("ID")
        table.add_column("Name")
        for i, d in enumerate(matches):
            table.add_row(str(i), d['id'], d.get('name',''))
        console.print(table)
        sel = Prompt.ask("Select by index (comma-separated)", default="")
        for part in sel.split(","):
            part = part.strip()
            if part.isdigit():
                i = int(part)
                if 0 <= i < len(matches):
                    dep = matches[i]
                    if dep not in selected:
                        selected.append(dep)
                        pool.remove(dep)
        print(f"[green]Selected: {[d['id'] for d in selected]}[/green]")
    return [d['id'] for d in selected]

def map_type(val):
    if isinstance(val, str): return "String"
    if isinstance(val, bool): return "Boolean"
    if isinstance(val, int): return "Integer"
    if isinstance(val, float): return "Double"
    return "Object"

def singularize(name):
    if name.endswith("s"):
        return name[:-1]
    return name

def generate_model_classes(json_obj, class_name, collected=None):
    if collected is None:
        collected = {}
    fields = []
    nested = {}
    for key, val in json_obj.items():
        if isinstance(val, dict):
            nested_class = key.capitalize()
            nested[nested_class] = val
            field_type = nested_class
            annotations = ["@NotNull", "@Valid"]
        elif isinstance(val, list):
            if val and isinstance(val[0], dict):
                nested_class = singularize(key).capitalize()
                nested[nested_class] = val[0]
                field_type = f"List<{nested_class}>"
                annotations = ["@NotNull", "@Valid"]
            else:
                if val:
                    prim = val[0]
                    jt = map_type(prim)
                else:
                    jt = "Object"
                field_type = f"List<{jt}>"
                annotations = ["@NotNull"]
        else:
            jt = map_type(val)
            field_type = jt
            if jt == "String":
                annotations = ["@NotBlank"]
            else:
                annotations = ["@NotNull"]
        annotations.append(f"@Schema(description=\"{key}\")")
        fields.append({'name':key,'type':field_type,'annotations':annotations})
    collected[class_name] = fields
    for nclass, njson in nested.items():
        generate_model_classes(njson, nclass, collected)
    return collected

def write_model_java(package, class_name, fields, base_dir):
    pkg = f"{package}.model"
    lines = [f"package {pkg};", ""]
    imports = set()
    for f in fields:
        for ann in f['annotations']:
            if ann.startswith("@NotNull"):
                imports.add("import javax.validation.constraints.NotNull;")
            if ann.startswith("@NotBlank"):
                imports.add("import javax.validation.constraints.NotBlank;")
            if ann.startswith("@Valid"):
                imports.add("import javax.validation.Valid;")
            if ann.startswith("@Schema"):
                imports.add("import io.swagger.v3.oas.annotations.media.Schema;")
        if "List<" in f['type']:
            imports.add("import java.util.List;")
    if imports:
        for imp in sorted(imports):
            lines.append(imp)
        lines.append("")
    lines.append(f"@Schema(description=\"{class_name}\")")
    lines.append(f"public class {class_name} " + "{")
    for f in fields:
        for ann in f['annotations']:
            lines.append(f"    {ann}")
        lines.append(f"    private {f['type']} {f['name']};")
        lines.append("")
    for f in fields:
        name = f['name']
        typ = f['type']
        ms = name[0].upper()+name[1:]
        lines.append(f"    public {typ} get{ms}() " + "{")
        lines.append(f"        return {name};")
        lines.append("    }")
        lines.append("")
        lines.append(f"    public void set{ms}({typ} {name}) " + "{")
        lines.append(f"        this.{name} = {name};")
        lines.append("    }")
        lines.append("")
    lines.append("}")
    path = os.path.join(base_dir, "src", "main", "java", *package.split("."), "model")
    os.makedirs(path, exist_ok=True)
    fp = os.path.join(path, f"{class_name}.java")
    with open(fp, "w") as f:
        f.write("\n".join(lines))

def write_controller(package, entity, res_fields, base_dir):
    plural = entity.lower() + "s"
    pkg = f"{package}.controller"
    lines = [f"package {pkg};", ""]
    imps = [
        "import org.springframework.web.bind.annotation.*;",
        "import org.springframework.http.ResponseEntity;",
        "import org.springframework.http.HttpStatus;",
        "import javax.validation.Valid;",
        f"import {package}.model.{entity}Request;",
        f"import {package}.model.{entity}Response;",
        "import io.swagger.v3.oas.annotations.Operation;",
        "import io.swagger.v3.oas.annotations.tags.Tag;",
        "import io.swagger.v3.oas.annotations.parameters.RequestBody;",
        "import io.swagger.v3.oas.annotations.responses.ApiResponse;",
        "import io.swagger.v3.oas.annotations.media.Content;",
        "import io.swagger.v3.oas.annotations.media.Schema;"
    ]
    lines.extend(imps); lines.append("")
    lines.append(f"@Tag(name=\"{entity}\")")
    lines.append("@RestController")
    lines.append(f"@RequestMapping(\"/api/v1/{plural}\")")
    lines.append(f"public class {entity}Controller " + "{")
    lines.append("")
    lines.append("    @Operation(summary=\"Create " + entity + "\", responses={")
    lines.append("        @ApiResponse(responseCode=\"201\", description=\"Created\", content=@Content(schema=@Schema(implementation="+entity+"Response.class)))")
    lines.append("    })")
    lines.append("    @PostMapping")
    lines.append(f"    public ResponseEntity<{entity}Response> create{entity}(@Valid @RequestBody {entity}Request request) " + "{")
    lines.append(f"        {entity}Response response = new {entity}Response();")
    for f in res_fields:
        if f['type'] in ["String","Integer","Double","Boolean"]:
            ms = f['name'][0].upper()+f['name'][1:]
            val = f['value']
            if isinstance(val, str):
                lit = f"\"{val}\""
            else:
                lit = str(val)
            lines.append(f"        response.set{ms}({lit});")
    lines.append("        return ResponseEntity.status(HttpStatus.CREATED).body(response);")
    lines.append("    }")
    lines.append("}")
    path = os.path.join(base_dir, "src", "main", "java", *package.split("."), "controller")
    os.makedirs(path, exist_ok=True)
    fp = os.path.join(path, f"{entity}Controller.java")
    with open(fp, "w") as f:
        f.write("\n".join(lines))

def write_test(package, entity, req_file, res_file, base_dir):
    plural = entity.lower() + "s"
    pkg = f"{package}.controller"
    path = os.path.join(base_dir, "src", "test", "java", *package.split("."), "controller")
    os.makedirs(path, exist_ok=True)
    fp = os.path.join(path, f"{entity}ControllerTest.java")
    lines = [f"package {pkg};", "",]
    lines += [
        "import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;",
        "import org.springframework.boot.test.context.SpringBootTest;",
        "import org.springframework.test.web.servlet.MockMvc;",
        "import org.springframework.beans.factory.annotation.Autowired;",
        "import org.junit.jupiter.api.Test;",
        "import org.springframework.http.MediaType;",
        "import java.nio.file.Files;",
        "import java.nio.file.Paths;",
        "",   
        "import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;",
        "import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;",
        "",   
        "@SpringBootTest",
        "@AutoConfigureMockMvc",
        f"public class {entity}ControllerTest " + "{",
        "",
        "    @Autowired",
        "    private MockMvc mockMvc;",
        "",
        "    @Test",
        f"    public void testCreate{entity}() throws Exception " + "{",
        f"        String requestJson = new String(Files.readAllBytes(Paths.get(\"src/test/resources/{req_file}\")));",
        f"        String responseJson = new String(Files.readAllBytes(Paths.get(\"src/test/resources/{res_file}\")));",
        f"        mockMvc.perform(post(\"/api/v1/{plural}\")",
        "            .contentType(MediaType.APPLICATION_JSON)",
        "            .content(requestJson))",
        "            .andExpect(status().isCreated())",
        "            .andExpect(content().json(responseJson));",
        "    }",
        "}"   
    ]
    with open(fp, "w") as f:
        f.write("\n".join(lines))

def main():
    console = Console()
    print("[bold green]Codex Spring Boot Starter CLI[/bold green]")
    # Application class name validation: CamelCase, starts with uppercase
    while True:
        app_name = Prompt.ask("Application class name (CamelCase)", default="DemoApp")
        if re.match(r"^[A-Z][A-Za-z0-9]*$", app_name):
            break
        print("[red]Invalid class name. Must start with uppercase letter and contain only alphanumeric characters.[/red]")
    # ArtifactId validation: lowercase, alphanumeric or hyphens
    while True:
        artifact_id = Prompt.ask("ArtifactId (module name)", default=app_name.lower())
        if re.match(r"^[a-z][a-z0-9-]*$", artifact_id):
            break
        print("[red]Invalid artifactId. Must start with lowercase letter and contain only lowercase letters, digits, or hyphens.[/red]")
    # GroupId validation: Java package name (lowercase, dot-separated)
    while True:
        group_id = Prompt.ask("GroupId (package name)", default=f"com.example.{artifact_id}")
        if re.match(r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9]*)*$", group_id):
            break
        print("[red]Invalid groupId. Must be a dot-separated Java package name in lowercase.[/red]")

    metadata = fetch_metadata()
    proto = select_option("Select build tool", metadata["type"]["values"])
    # filter out SNAPSHOT and RC boot versions, prefer stable releases
    raw_boots = metadata.get("bootVersion", {}).get("values", [])
    boot_choices = [b for b in raw_boots if "SNAPSHOT" not in b.get("name", "") and "RC" not in b.get("name", "")]
    if not boot_choices:
        boot_choices = raw_boots
    boot = select_option("Select Spring Boot version", boot_choices)
    java = select_option("Select Java version", metadata.get("javaVersion", {}).get("values", []))
    # Flatten dependency categories into a single list for fuzzy search
    all_deps = []
    for cat in metadata.get("dependencies", {}).get("values", []):
        # each category has its own 'values' list
        all_deps.extend(cat.get("values", []))
    deps = fuzzy_select_dependencies(all_deps)

    # Validate JSON request directory
    while True:
        req_dir = Prompt.ask("JSON request directory", default="request_json")
        if os.path.isdir(req_dir):
            tmp = [f for f in os.listdir(req_dir) if f.endswith(".json")]
            if tmp:
                break
            print(f"[red]No JSON files found in {req_dir}[/red]")
        else:
            print(f"[red]{req_dir} is not a directory[/red]")
    # Validate JSON response directory
    while True:
        res_dir = Prompt.ask("JSON response directory", default="response_json")
        if os.path.isdir(res_dir):
            tmp = [f for f in os.listdir(res_dir) if f.endswith(".json")]
            if tmp:
                break
            print(f"[red]No JSON files found in {res_dir}[/red]")
        else:
            print(f"[red]{res_dir} is not a directory[/red]")

    print("\n[bold]Summary:[/bold]")
    print(f"App: {app_name}, ArtifactId: {artifact_id}, GroupId: {group_id}")
    print(f"Build: {proto}, Boot: {boot}, Java: {java}")
    print(f"Dependencies: {deps}")
    print(f"Request dir: {req_dir}, Response dir: {res_dir}\n")
    if not Confirm.ask("Proceed with generation?"):
        sys.exit(0)

    url = ("https://start.spring.io/starter.zip"
           f"?type={proto}&language=java"
           f"&bootVersion={boot}&javaVersion={java}"
           f"&groupId={group_id}&artifactId={artifact_id}"
           f"&name={app_name}&dependencies={','.join(deps)}")
    # Determine output directory and resume or overwrite
    out_dir = os.path.abspath(artifact_id)
    resume_flag = False
    if os.path.exists(out_dir):
        choice = Prompt.ask(
            f"Directory {out_dir} already exists. [R]esume previous session, [O]verwrite?",
            choices=["R","O"], default="R"
        )
        if choice.upper() == "O":
            shutil.rmtree(out_dir)
        else:
            resume_flag = True
    # Fetch and extract starter if not resuming
    if not resume_flag:
        print("[blue]Fetching project from Spring Initializr...[/blue]")
        r = requests.get(url); r.raise_for_status()
        z = zipfile.ZipFile(io.BytesIO(r.content))
        os.makedirs(out_dir)
        z.extractall(out_dir)
        print(f"[green]Project created at {out_dir}[/green]")
    else:
        print(f"[yellow]Resuming session in existing directory {out_dir}[/yellow]")

    test_res = os.path.join(out_dir, "src", "test", "resources")
    os.makedirs(test_res, exist_ok=True)
    req_files = [f for f in os.listdir(req_dir) if f.endswith(".json")]
    res_files = [f for f in os.listdir(res_dir) if f.endswith(".json")]
    for req in req_files:
        pre = os.path.splitext(req)[0]
        matches = [f for f in res_files if os.path.splitext(f)[0].startswith(pre)]
        if not matches:
            print(f"[yellow]No response file for {req}[/yellow]")
            continue
        resf = matches[0]
        shutil.copy(os.path.join(req_dir, req), os.path.join(test_res, req))
        shutil.copy(os.path.join(res_dir, resf), os.path.join(test_res, resf))
        req_json = json.load(open(os.path.join(req_dir, req)))
        res_json = json.load(open(os.path.join(res_dir, resf)))
        entity = pre
        req_models = generate_model_classes(req_json, entity+"Request")
        res_models = generate_model_classes(res_json, entity+"Response")
        for cls, fields in req_models.items():
            write_model_java(group_id, cls, fields, out_dir)
        for cls, fields in res_models.items():
            write_model_java(group_id, cls, fields, out_dir)
        # controller and tests
        res_fields = [{'name':k,'type':map_type(v) if not isinstance(v,(dict,list)) else 'Object','value':v} for k,v in res_json.items()]
        write_controller(group_id, entity, res_fields, out_dir)
        write_test(group_id, entity, req, resf, out_dir)

    # Git handling: initialize or commit resumed changes
    if resume_flag:
        print("[blue]Committing resumed changes...[/blue]")
        subprocess.run(["git","add","."], cwd=out_dir)
        subprocess.run(["git","commit","-m","Resume JSON code generation"], cwd=out_dir)
    else:
        print("[blue]Initializing git repository...[/blue]")
        subprocess.run(["git","init"], cwd=out_dir)
        subprocess.run(["git","add","."], cwd=out_dir)
        subprocess.run(["git","commit","-m","Initial commit by Codex CLI"], cwd=out_dir)

    print("[blue]Building project...[/blue]")
    if proto.startswith("maven"):
        subprocess.run(["mvn","clean","install"], cwd=out_dir)
    else:
        subprocess.run(["./gradlew","clean","build"], cwd=out_dir)

    print(f"[bold green]Done! Project is ready in {out_dir}[/bold green]")

if __name__ == "__main__":
    main()