const path = require('path');

module.exports = {
  entry: './src/index.tsx',
  output: {
    filename: 'stats-bundle.js',
    path: path.resolve(__dirname, '..', 'static'),
    library: { type: 'umd', name: 'StudyStats' },
    globalObject: 'this',
  },
  resolve: {
    extensions: ['.ts', '.tsx', '.js', '.jsx'],
  },
  module: {
    rules: [
      {
        test: /\.tsx?$/,
        use: 'ts-loader',
        exclude: /node_modules/,
      },
      {
        test: /\.css$/,
        use: ['style-loader', 'css-loader'],
      },
    ],
  },
  externals: {},
};
