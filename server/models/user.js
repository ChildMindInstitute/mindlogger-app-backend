'use strict';
import bcrypt from 'bcrypt';

module.exports = (sequelize, DataTypes) => {
  var User = sequelize.define('user', {
    first_name: DataTypes.STRING,
    last_name: DataTypes.STRING,
    email: DataTypes.STRING,
    password: DataTypes.STRING,
    role: DataTypes.STRING,
    newsletter: DataTypes.BOOLEAN,
    verify_token: DataTypes.STRING,
    access_token: DataTypes.STRING,
    status: {
        type: DataTypes.STRING,
        defaultValue: 'active'
    }
  }, {underscored: true});

  User.associate = (models) => {
    User.hasMany(models.Act, {as: 'ownActs'});
    User.belongsToMany(models.Act, { through: 'UserAct'});
    User.hasMany(models.Answer);
    User.belongsTo(models.Organization);
  }
  
  User.generateToken = () => {
    let chars, token;
    chars = "_!abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890",
        token = new Date().getTime() + '_';
    for (let x = 0; x < 16; x++) {
        let i = Math.floor(Math.random() * 62);
        token += chars.charAt(i);
    }
    return token;
  }
  User.generateTempToken = () => {
      let chars, temp = '';
      chars = "_!abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890";
      for (let x = 0; x < 16; x++) {
          let i = Math.floor(Math.random() * 62);
          temp += chars.charAt(i);
      }
      return temp;
  }
  User.deleteUser = (user) => {
      return user.influencers.update({status: 'deleted'})
          .then(() => {
              return user.brands.update({status: 'deleted'});
          })
          .then(() => {
              return User.update({status: 'deleted'}, {where:{id: user.id}});
          })
          .then(() => {
              return true;
          })
  }
  return User;
};