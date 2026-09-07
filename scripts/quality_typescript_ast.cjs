#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");

const payload = JSON.parse(fs.readFileSync(0, "utf8"));
const ts = require(path.join(payload.root, "frontend", "node_modules", "typescript"));

function namedOf(node) {
  if (ts.isConstructorDeclaration(node)) {
    return { text: "constructor", computed: false };
  }
  if (!node.name) return null;
  if (ts.isIdentifier(node.name) || ts.isPrivateIdentifier(node.name)) {
    return { text: node.name.text, computed: false };
  }
  if (ts.isStringLiteral(node.name) || ts.isNumericLiteral(node.name)) {
    return { text: node.name.text, computed: false };
  }
  return { text: null, computed: true };
}

function classOrModuleName(node) {
  if (ts.isClassDeclaration(node) || ts.isClassExpression(node)) {
    return node.name && ts.isIdentifier(node.name) ? node.name.text : null;
  }
  if (ts.isModuleDeclaration(node) && node.name && ts.isIdentifier(node.name)) {
    return node.name.text;
  }
  return undefined;
}

function namedFunction(node) {
  if (
    ts.isFunctionDeclaration(node) ||
    ts.isMethodDeclaration(node) ||
    ts.isGetAccessorDeclaration(node) ||
    ts.isSetAccessorDeclaration(node) ||
    ts.isConstructorDeclaration(node)
  ) {
    const named = namedOf(node);
    if (!named) {
      return { name: null, target: node, computed: false, anonymous: true };
    }
    if (named.computed) {
      return { name: named.text, target: node, computed: true, anonymous: false };
    }
    return { name: named.text, target: node, computed: false, anonymous: false };
  }
  if (
    ts.isVariableDeclaration(node) &&
    ts.isIdentifier(node.name) &&
    node.initializer &&
    (ts.isArrowFunction(node.initializer) || ts.isFunctionExpression(node.initializer))
  ) {
    return {
      name: node.name.text,
      target: node.initializer,
      computed: false,
      anonymous: false,
    };
  }
  return null;
}

function record(fn, sf, file, containers, out) {
  const start = sf.getLineAndCharacterOfPosition(fn.target.getStart(sf, false));
  const end = sf.getLineAndCharacterOfPosition(fn.target.end);
  out.push({
    path: file,
    name: fn.name,
    containers: containers.slice(),
    line: start.line + 1,
    column: start.character + 1,
    end_line: end.line + 1,
    length: end.line - start.line + 1,
    computed: fn.computed,
    anonymous: fn.anonymous || !fn.name,
  });
}

function walk(node, sf, file, containers, out) {
  ts.forEachChild(node, (child) => visit(child, sf, file, containers, out));
}

function visit(node, sf, file, containers, out) {
  const scope = classOrModuleName(node);
  if (scope !== undefined) {
    if (scope) containers.push(scope);
    walk(node, sf, file, containers, out);
    if (scope) containers.pop();
    return;
  }
  if (
    ts.isVariableDeclaration(node) &&
    ts.isIdentifier(node.name) &&
    node.initializer
  ) {
    const fn = namedFunction(node);
    if (fn) {
      record(fn, sf, file, containers, out);
      containers.push(fn.name);
      walk(fn.target, sf, file, containers, out);
      containers.pop();
      return;
    }
    containers.push(node.name.text);
    visit(node.initializer, sf, file, containers, out);
    containers.pop();
    return;
  }
  if (
    (ts.isPropertyAssignment(node) || ts.isPropertyDeclaration(node)) &&
    node.name &&
    ts.isIdentifier(node.name)
  ) {
    const init = node.initializer;
    if (init && (ts.isArrowFunction(init) || ts.isFunctionExpression(init))) {
      record(
        { name: node.name.text, target: init, computed: false, anonymous: false },
        sf,
        file,
        containers,
        out
      );
      containers.push(node.name.text);
      walk(init, sf, file, containers, out);
      containers.pop();
      return;
    }
    if (init) {
      containers.push(node.name.text);
      visit(init, sf, file, containers, out);
      containers.pop();
      return;
    }
  }
  const fn = namedFunction(node);
  if (fn) {
    record(fn, sf, file, containers, out);
    if (fn.name && !fn.computed && !fn.anonymous) {
      containers.push(fn.name);
      walk(node, sf, file, containers, out);
      containers.pop();
      return;
    }
  }
  walk(node, sf, file, containers, out);
}

const out = [];
for (const file of payload.files) {
  const kind = file.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const text = fs.readFileSync(path.join(payload.root, file), "utf8");
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, kind);
  visit(sf, sf, file, [], out);
}
process.stdout.write(JSON.stringify({ functions: out }));
