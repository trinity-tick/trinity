// fix_dbtags.go — AST 级 db tags 迁移工具（EXECUTION 447 专项）
// 给有 gorm tag 缺 db tag 的 struct 字段补 db:"列名"（json 名优先，字段名 snake_case 兜底）。
// 用法: go run fix_dbtags.go <glob 包目录...>
package main

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strings"
)

func toSnake(n string) string {
	var out []rune
	rs := []rune(n)
	for i, ch := range rs {
		if ch >= 'A' && ch <= 'Z' && i > 0 {
			out = append(out, '_')
		}
		if ch >= 'A' && ch <= 'Z' {
			ch = ch + 32
		}
		out = append(out, ch)
	}
	return string(out)
}

func processFile(path string) (int, error) {
	fset := token.NewFileSet()
	f, err := parser.ParseFile(fset, path, nil, parser.ParseComments)
	if err != nil {
		return 0, err
	}
	type edit struct {
		line int
		old  string
		new  string
	}
	var edits []edit
	src, err := os.ReadFile(path)
	if err != nil {
		return 0, err
	}
	srcLines := strings.Split(string(src), "
")

	ast.Inspect(f, func(n ast.Node) bool {
		st, ok := n.(*ast.StructType)
		if !ok {
			return true
		}
		for _, field := range st.Fields.List {
			if field.Tag == nil {
				continue
			}
			tagLit := field.Tag.Value // 含反引号
			if strings.Contains(tagLit, `db:"`) || !strings.Contains(tagLit, `gorm:"`) {
				continue
			}
			if len(field.Names) == 0 {
				continue
			}
			fieldName := field.Names[0].Name
			col := toSnake(fieldName)
			if jm := strings.Index(tagLit, `json:"`); jm >= 0 {
				rest := tagLit[jm+len(`json:"`):]
				if e := strings.Index(rest, `"`); e >= 0 {
					col = rest[:e]
				}
			}
			inner := strings.Trim(tagLit, "`")
			newTag := `db:"` + col + `" ` + inner
			pos := fset.Position(field.Tag.Pos())
			lineIdx := pos.Line - 1
			if lineIdx < 0 || lineIdx >= len(srcLines) {
				continue
			}
			line := srcLines[lineIdx]
			i1 := strings.Index(line, "`")
			i2 := strings.LastIndex(line, "`")
			if i1 < 0 || i2 <= i1 {
				continue
			}
			newLine := line[:i1] + "`" + newTag + "`" + line[i2+1:]
			edits = append(edits, edit{line: lineIdx, old: line, new: newLine})
			fixed++
			_ = fixed
		}
		return true
	})
	// 应用编辑（按行号从后往前避免位移；同一行只编辑一次）
	applied := map[int]bool{}
	count := 0
	for i := len(edits) - 1; i >= 0; i-- {
		ed := edits[i]
		if applied[ed.line] {
			continue
		}
		srcLines[ed.line] = ed.new
		applied[ed.line] = true
		count++
	}
	if count == 0 {
		return 0, nil
	}
	return count, os.WriteFile(path, []byte(strings.Join(srcLines, "
")), 0644)
}

var totalFixed int

func main() {
	for _, dir := range os.Args[1:] {
		files, _ := filepath.Glob(filepath.Join(dir, "*.go"))
		for _, f := range files {
			if strings.HasSuffix(f, "_test.go") {
				continue
			}
			fixed, err := processFile(f)
			if err != nil {
				fmt.Println("ERR", f, err)
				continue
			}
			totalFixed += fixed
			if fixed > 0 {
				fmt.Printf("%s: %d fields\n", f, fixed)
			}
		}
	}
	fmt.Println("TOTAL:", totalFixed)
}
